#!/usr/bin/env python3
"""agent_runner — универсальный MCP-раннер агентов.

Один инструмент для любой задачи: спека (YAML / JSON / markdown) описывает
КЕМ и КАК запускать, задача приходит в вызове. Компания — не обязательна,
она подключается как набор спек (import_company_specs).

Что даёт:
- list_agents / describe_agent / validate_agent
- run_agent (фон, detached процесс, переживает закрытие клиента)
- get_run / list_runs / cancel_run
- save_agent / delete_agent — новые роли без перезапуска сервера
- import_company_specs — роли компании как спеки (через company.py call)

Запуск (stdio):
  uvx --with fastmcp --with pyyaml python server.py
  python server.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP

from runnerlib import pid_alive, spawn_detached, spawn_scheduled

import spec as specmod
from runner import kill_tree, now

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("AGENT_RUNNER_HOME") or (Path.home() / ".agent_runner")).expanduser()
HOME.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("SPAWN_LOG", str(HOME / "spawn.log"))
DB_PATH = Path(os.environ.get("AGENT_RUNNER_DB") or (HOME / "runs.db"))
STATE = HOME
TASKS = STATE / "tasks"
TASKS.mkdir(parents=True, exist_ok=True)

SPEC_DIRS = [HERE / "specs", STATE / "specs"]
for extra in (os.environ.get("AGENT_RUNNER_SPECS") or "").split(os.pathsep):
    if extra.strip():
        SPEC_DIRS.append(Path(extra).expanduser())
USER_SPECS = STATE / "specs"

MAX_TASK = 20000
MAX_CONCURRENT = int(os.environ.get("AGENT_RUNNER_MAX_CONCURRENT") or 3)
TERMINAL = ("ok", "fail", "timeout", "cancelled", "lost")

mcp = FastMCP("agent-runner")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            runtime TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            task TEXT NOT NULL DEFAULT '',
            params TEXT NOT NULL DEFAULT '{}',
            argv TEXT NOT NULL DEFAULT '[]',
            use_shell INTEGER NOT NULL DEFAULT 0,
            cwd TEXT NOT NULL DEFAULT '',
            env TEXT NOT NULL DEFAULT '{}',
            timeout_s INTEGER NOT NULL DEFAULT 900,
            tokens_regex TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            ended_at TEXT NOT NULL DEFAULT '',
            exit_code INTEGER,
            tokens INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '',
            log TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            runner_pid INTEGER
        )"""
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if "runner_pid" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN runner_pid INTEGER")
    conn.commit()
    return conn


def _spec_paths() -> list[Path]:
    out: list[Path] = []
    for d in SPEC_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() in (".yaml", ".yml", ".json", ".md", ".markdown") and p.is_file():
                out.append(p)
    return out


def _load(name: str) -> dict[str, Any]:
    for path in _spec_paths():
        if path.stem == name or path.stem == name.split(".")[0]:
            return specmod.normalize(specmod.read_spec_file(path))
    raise ValueError("агент не найден: " + name)


def _reap() -> None:
    """Помечает зависшие run-ы, чтобы не блокировать очередь."""
    cutoff = datetime.now(timezone.utc).timestamp() - 4 * 3600
    with db() as conn:
        rows = conn.execute("SELECT id, started_at, timeout_s, status, runner_pid FROM runs "
                            "WHERE status IN ('queued','running')").fetchall()
        for r in rows:
            rp = r["runner_pid"]
            if rp and not pid_alive(int(rp)):
                conn.execute("UPDATE runs SET status='lost', ended_at=?, error=? WHERE id=?",
                             (now(), "раннер пропал: клиент убил дерево процессов. "
                                     "Повтори run или включи AGENT_RUNNER_SPAWN=schtasks",
                              r["id"]))
                continue
            stamp = r["started_at"] or ""
            try:
                ts = datetime.fromisoformat(stamp).timestamp() if stamp else 0
            except Exception:
                ts = 0
            limit = max(int(r["timeout_s"] or 900), 60) + 600
            if r["status"] == "running" and ts and time.time() - ts > limit:
                conn.execute("UPDATE runs SET status='timeout', ended_at=?, error=? WHERE id=?",
                             (now(), "раннер не отвечает", r["id"]))
            elif r["status"] == "queued" and ts and ts < cutoff:
                conn.execute("UPDATE runs SET status='fail', ended_at=?, error=? WHERE id=?",
                             (now(), "запуск не состоялся", r["id"]))
        conn.commit()


def _active_count() -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM runs WHERE status IN ('queued','running')").fetchone()
    return int(row["c"])


def _spawn(run_id: str, mode: str = "") -> int:
    runner = Path(__file__).resolve().parent / "runner.py"
    env = os.environ.copy()
    env["AGENT_RUNNER_DB"] = str(DB_PATH)
    logs = STATE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    errlog = open(logs / (run_id + ".runner.log"), "a", encoding="utf-8")
    argv = [sys.executable, str(runner), run_id]
    mode = (mode or os.environ.get("AGENT_RUNNER_SPAWN", "detach")).lower()
    if mode in ("schtasks", "scheduler"):
        spawn_scheduled(argv, "agentrun_" + run_id, str(ROOT), STATE, env)
        with db() as conn:
            conn.execute("UPDATE runs SET runner_pid=0 WHERE id=?", (run_id,))
            conn.commit()
        return 0
    try:
        p = spawn_detached(argv, cwd=HERE, env=env, stderr=errlog)
    finally:
        errlog.close()
    with db() as conn:
        conn.execute("UPDATE runs SET pid=?, runner_pid=? WHERE id=?",
                     (p.pid, p.pid, run_id))
        conn.commit()
    return p.pid


def _wait(run_id: str, wait_s: int) -> dict[str, Any]:
    deadline = time.time() + max(0, wait_s)
    while True:
        row = get_run(run_id)
        if row["status"] in TERMINAL or time.time() >= deadline:
            return row
        time.sleep(2)


@mcp.tool()
def list_agents(tag: str = "") -> list[dict[str, Any]]:
    """Все доступные агенты: имя, runtime, описание, теги."""
    out = []
    seen = set()
    for path in _spec_paths():
        try:
            s = specmod.normalize(specmod.read_spec_file(path))
        except Exception as exc:
            out.append({"name": path.stem, "error": str(exc)[:300], "path": str(path)})
            continue
        if s["name"] in seen:
            continue
        if tag and tag not in s["tags"]:
            continue
        seen.add(s["name"])
        out.append({"name": s["name"], "title": s.get("title") or "",
                    "runtime": s["runtime"], "tags": s["tags"],
                    "description": (s.get("description") or "")[:300],
                    "path": str(path)})
    return sorted(out, key=lambda x: x.get("name") or "")


@mcp.tool()
def describe_agent(name: str) -> dict[str, Any]:
    """Полное описание агента: команда, параметры, лимиты."""
    s = _load(name)
    return {k: s[k] for k in ("name", "title", "description", "runtime", "tags",
                              "params", "task_param", "project_param", "timeout_s",
                              "max_turns", "output", "session", "cwd", "base_dir")}


@mcp.tool()
def validate_agent(name: str = "", spec: dict | None = None, task: str = "проверка") -> dict[str, Any]:
    """Проверить спеку и показать argv без запуска."""
    try:
        s = specmod.normalize(spec or _load(name))
        tmp = TASKS / "_validate.md"
        tmp.write_text(task, encoding="utf-8", newline="\n")
        argv, use_shell, cwd, _ = specmod.build(
            s, task=task, project="", task_file=tmp, run_id="validate")
        return {"ok": True, "runtime": s["runtime"], "argv": argv,
                "use_shell": use_shell, "cwd": cwd}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:800]}


@mcp.tool()
def save_agent(spec: dict) -> dict[str, Any]:
    """Сохранить нового агента (словарь спеки или YAML-строка в поле)."""
    if "yaml" in spec and len(spec) == 1:
        spec = yaml.safe_load(spec["yaml"]) or {}
    s = specmod.normalize(spec, base_dir=USER_SPECS)
    USER_SPECS.mkdir(parents=True, exist_ok=True)
    path = USER_SPECS / (s["name"] + ".yaml")
    path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
                    encoding="utf-8", newline="\n")
    return {"ok": True, "name": s["name"], "path": str(path)}


@mcp.tool()
def delete_agent(name: str) -> dict[str, Any]:
    """Удалить только пользовательского агента из state/specs."""
    path = USER_SPECS / (name + ".yaml")
    if not path.exists():
        raise ValueError("пользовательский агент не найден: " + name)
    path.unlink()
    return {"ok": True, "deleted": name}


@mcp.tool()
def run_agent(agent: str = "", task: str = "", project: str = "",
              params: dict[str, str] | None = None, spec: dict | None = None,
              wait_s: int = 0, timeout_s: int = 0, mode: str = "") -> dict[str, Any]:
    """Запустить агента в фоне. wait_s>0 — дождаться результата.

    agent — имя сохранённой спеки; spec — разовая спека вместо файла.
    mode: detach (по умолчанию) или schtasks — переживает закрытие клиента.
    """
    _reap()
    if _active_count() >= MAX_CONCURRENT:
        raise ValueError("слишком много активных run-ов (лимит %d), подожди или отмени" % MAX_CONCURRENT)
    if not task.strip():
        raise ValueError("нужна задача")
    if len(task) > MAX_TASK:
        raise ValueError("задача слишком длинная")
    if project:
        p = Path(project).expanduser().resolve()
        if not p.is_dir():
            raise ValueError("project не существует: " + str(p))
        project = str(p)
    s = specmod.normalize(spec or _load(agent))
    if params:
        s["params"].update({str(k): str(v) for k, v in params.items()})
    if timeout_s:
        s["timeout_s"] = int(timeout_s)

    run_id = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    task_file = TASKS / (run_id + ".md")
    header = "# Задача\n\n- агент: " + s["name"] + "\n- проект: " + (project or "-") + "\n\n"
    task_file.write_text(header + task.rstrip() + "\n", encoding="utf-8", newline="\n")
    argv, use_shell, cwd, env = specmod.build(s, task=task, project=project,
                                              task_file=task_file, run_id=run_id)
    overlay = {k: v for k, v in env.items() if os.environ.get(k) != v}
    with db() as conn:
        conn.execute(
            "INSERT INTO runs (id, agent, runtime, project, task, params, argv, use_shell, "
            "cwd, env, timeout_s, tokens_regex, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',?)",
            (run_id, s["name"], s["runtime"], project, task,
             json.dumps(s["params"], ensure_ascii=False),
             json.dumps(argv, ensure_ascii=False), 1 if use_shell else 0,
             cwd, json.dumps(overlay, ensure_ascii=False),
             int(s["timeout_s"]), str(s.get("tokens_regex") or ""), now()))
        conn.commit()
    pid = _spawn(run_id, mode)
    out = {"run_id": run_id, "agent": s["name"], "status": "queued", "pid": pid,
           "task_file": str(task_file)}
    if wait_s > 0:
        row = _wait(run_id, min(int(wait_s), 3600))
        out.update({"status": row["status"], "tokens": row["tokens"],
                    "result": row["result"], "log": row["log"],
                    "error": row["error"][-1500:]})
    return out


@mcp.tool()
def get_run(run_id: str, include_log: bool = False) -> dict[str, Any]:
    """Статус и результат run-а."""
    _reap()
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("run не найден: " + run_id)
    d = dict(row)
    d["result"] = json.loads(d["result"]) if d["result"] else None
    d["task"] = d["task"][:500]
    if not include_log:
        d["error"] = d["error"][-1500:]
    return d


@mcp.tool()
def list_runs(limit: int = 20, status: str = "") -> list[dict[str, Any]]:
    """Последние run-ы, можно фильтровать по статусу."""
    limit = max(1, min(int(limit), 100))
    sql = ("SELECT id, agent, runtime, project, status, created_at, ended_at, exit_code, "
           "tokens, substr(task,1,160) task FROM runs")
    args: list[Any] = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def cancel_run(run_id: str) -> dict[str, Any]:
    """Отменить run: статус cancelled + убить процесс."""
    with db() as conn:
        row = conn.execute("SELECT status, pid FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("run не найден: " + run_id)
        if row["status"] in TERMINAL:
            return {"run_id": run_id, "status": row["status"], "changed": False}
        conn.execute("UPDATE runs SET status='cancelled', ended_at=? WHERE id=?",
                     (now(), run_id))
        conn.commit()
    if row["pid"]:
        kill_tree(int(row["pid"]))
    return {"run_id": run_id, "status": "cancelled", "changed": True}


@mcp.tool()
def import_company_specs() -> dict[str, Any]:
    """Сгенерировать спеки из реестра компании (через company.py call)."""
    company_root = Path(os.environ.get("COMPANY_ROOT") or HERE)
    company_db = company_root / "state" / "company.db"
    company_py = company_root / "company.py"
    if not company_db.exists() or not company_py.exists():
        raise ValueError("компания не найдена: задай COMPANY_ROOT "
                         "(нужны company.py и state/company.db)")
    conn = sqlite3.connect(company_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT name, kind, status, recipe_path FROM roles "
                        "WHERE status IN ('active','probation')").fetchall()
    conn.close()
    out_dir = USER_SPECS / "company"
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for r in rows:
        body = {
            "name": "company_" + r["name"],
            "title": "Компания: " + r["name"],
            "description": "Роль компании через company.py call (учёт токенов, lock, сессии)",
            "runtime": "shell",
            "command": ["python", str(company_py), "call",
                        "--role", r["name"], "--project", "{project}",
                        "--task", "{task}", "--fresh",
                        "--budget-tokens", "600000", "--timeout", "1800"],
            "cwd": str(company_root),
            "timeout_s": 1900,
            "tags": ["company", r["kind"] or "role"],
        }
        (out_dir / (body["name"] + ".yaml")).write_text(
            yaml.safe_dump(body, allow_unicode=True, sort_keys=False),
            encoding="utf-8", newline="\n")
        made.append(body["name"])
    return {"ok": True, "count": len(made), "agents": made, "dir": str(out_dir),
            "note": "запуск: run_agent(agent='company_programmer', task=..., project=...)"}


if __name__ == "__main__":
    if "--run-run" in sys.argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from runner import run as run_one
        run_one(sys.argv[sys.argv.index("--run-run") + 1])
    elif "--selftest" in sys.argv:
        print(json.dumps({"agents": list_agents(), "runs": list_runs(5)},
                         ensure_ascii=False, indent=2))
    else:
        mcp.run()

#!/usr/bin/env python3
"""Исполнитель одного run для agent_runner. Только stdlib.

Все параметры запуска (argv, cwd, env, timeout) уже лежат в БД, поэтому
раннеру не нужны ни fastmcp, ни yaml, ни сама спека.

  python runner.py <run_id>
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
from runnerlib import cleanup_scheduled

HOME = Path(os.environ.get("AGENT_RUNNER_HOME") or (Path.home() / ".agent_runner")).expanduser()
DB_PATH = Path(os.environ.get("AGENT_RUNNER_DB") or (HOME / "runs.db"))
LOGS = HOME / "logs"

TERMINAL = ("ok", "fail", "timeout", "cancelled")


def trace(run_id: str, msg: str) -> None:
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / (run_id + ".trace"), "a", encoding="utf-8") as f:
            f.write(now() + " " + msg + "\n")
    except Exception:
        pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True)
    else:
        try:
            os.killpg(os.getpgid(pid), 15)
        except Exception:
            pass


def extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    dec = json.JSONDecoder()
    pos = 0
    while True:
        i = text.find("{", pos)
        if i < 0:
            return None
        try:
            obj, _ = dec.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        pos = i + 1


def parse(stdout: str, regex: str) -> tuple[dict, int]:
    data = extract_json(stdout)
    tokens = 0
    text = stdout or ""
    if isinstance(data, dict) and "messages" in data:
        last = ""
        for msg in data.get("messages") or []:
            if msg.get("role") == "assistant":
                for c in msg.get("content") or []:
                    if c.get("type") == "text" and c.get("text"):
                        last = c["text"]
        text = last
        tokens = int((data.get("metadata") or {}).get("total_tokens") or 0)
        return {"status": data.get("status") or "", "text": last, "json": data}, tokens
    if not tokens:
        m = re.search(regex or r"(\d+)\s+токенов", stdout or "")
        if m:
            tokens = int(m.group(1))
    return {"status": "", "text": text, "json": data}, tokens


def run(run_id: str) -> None:
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return
        claimed = conn.execute(
            "UPDATE runs SET status='running', started_at=? WHERE id=? AND status='queued'",
            (now(), run_id)).rowcount
        conn.commit()
    if not claimed:
        return
    trace(run_id, "claimed")

    argv = json.loads(row["argv"])
    env = os.environ.copy()
    env.update(json.loads(row["env"] or "{}"))
    cwd = row["cwd"] or str(HERE)
    timeout_s = int(row["timeout_s"] or 900)
    use_shell = bool(row["use_shell"])

    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / (run_id + ".log")
    with open(log_path, "w", encoding="utf-8", newline="\n") as logf:
        logf.write("CWD: " + cwd + "\nCMD: " + json.dumps(argv, ensure_ascii=False) + "\n\n")
        logf.flush()
        try:
            proc = subprocess.Popen(argv, shell=use_shell, cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace",
                                    **({"creationflags": 0x00000200} if os.name == "nt" else {}))
        except Exception as exc:
            with db() as conn:
                conn.execute("UPDATE runs SET status='fail', ended_at=?, error=? WHERE id=?",
                             (now(), "spawn: " + str(exc)[:1500], run_id))
                conn.commit()
            return
        with db() as conn:
            conn.execute("UPDATE runs SET pid=? WHERE id=?", (proc.pid, run_id))
            conn.commit()
        trace(run_id, "child pid=" + str(proc.pid))
        try:
            out, _ = proc.communicate(timeout=timeout_s)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            out, _ = proc.communicate()
            code = 124
        logf.write(out or "")

    trace(run_id, "child done code=" + str(code))
    parsed, tokens = parse(out or "", row["tokens_regex"] or "")
    if code == 124:
        status = "timeout"
    elif code == 0:
        bad = str((parsed.get("json") or {}).get("status") or "").lower()
        status = "fail" if bad in ("fail", "blocked", "error") else "ok"
    else:
        status = "fail"
    with db() as conn:
        conn.execute(
            "UPDATE runs SET status=?, ended_at=?, exit_code=?, tokens=?, result=?, "
            "log=?, error=? WHERE id=?",
            (status, now(), code, tokens, json.dumps(parsed, ensure_ascii=False),
             str(log_path), "" if status == "ok" else (out or "")[-4000:], run_id))
        conn.commit()
    trace(run_id, "done " + status)
    cleanup_scheduled()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: runner.py <run_id>")
    run(sys.argv[1])

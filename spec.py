"""AgentSpec — описание любого агента, которого можно запустить из CLI.

Никакой привязки к компании. Спека — это словарь (YAML/JSON/MD), который
превращается в argv + env + рабочий каталог.

Поддерживаемые runtime:
  shell              — любая команда/CLI (goose, claude, aider, python ...)
  goose_recipe       — goose run --recipe <file> --params ...
  goose_instructions — goose run -i <file> (промт из markdown-тела спеки)

Поля спеки (все необязательные, кроме name и одного из command/recipe):
  name, title, description, tags
  runtime, command (str|list), recipe, instructions
  params (dict)      статические --params для goose / {подстановки} для shell
  task_param         имя goose-параметра, куда кладётся текст задачи (по умолч. task)
  project_param      имя goose-параметра для пути проекта (по умолч. project_path)
  session            true → не добавлять --no-session (по умолч. false)
  max_turns, timeout_s, output (json|text), tokens_regex
  env, cwd, pass_full_task
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

import yaml

RUNTIMES = ("shell", "goose_recipe", "goose_instructions")
BUILTINS = ("task", "task_file", "project", "spec_dir", "name", "run_id")


def read_spec_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text) or {}
    elif suffix == ".json":
        data = json.loads(text)
    elif suffix in (".md", ".markdown"):
        data, body = {}, text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                data = yaml.safe_load(parts[1]) or {}
                body = parts[2]
        data = dict(data)
        data.setdefault("runtime", "goose_instructions")
        data.setdefault("instructions", body.strip())
    else:
        raise ValueError("неизвестный формат спеки: " + path.name)
    if not isinstance(data, dict):
        raise ValueError("спека должна быть словарём: " + path.name)
    data.setdefault("name", path.stem)
    data["_path"] = str(path)
    return data


def normalize(spec: dict[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    s = dict(spec)
    s["name"] = str(s.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", s["name"]):
        raise ValueError("имя агента: только буквы, цифры, _ . - (до 64 символов)")
    runtime = str(s.get("runtime") or "").strip()
    if not runtime:
        if s.get("recipe"):
            runtime = "goose_recipe"
        elif s.get("instructions"):
            runtime = "goose_instructions"
        else:
            runtime = "shell"
    if runtime not in RUNTIMES:
        raise ValueError("runtime должен быть одним из: " + ", ".join(RUNTIMES))
    s["runtime"] = runtime

    base = Path(s.get("_path")).parent if s.get("_path") else (base_dir or Path.cwd())
    s["base_dir"] = str(base)

    if runtime == "goose_recipe":
        if not s.get("recipe"):
            raise ValueError("для goose_recipe нужен recipe")
        recipe = Path(str(s["recipe"]))
        s["recipe"] = str(recipe if recipe.is_absolute() else (base / recipe).resolve())
        if not Path(s["recipe"]).is_file():
            raise ValueError("рецепт не найден: " + s["recipe"])
    elif runtime == "goose_instructions":
        if not str(s.get("instructions") or "").strip():
            raise ValueError("для goose_instructions нужен текст instructions")
    else:
        if not s.get("command"):
            raise ValueError("для shell нужен command")
        if isinstance(s["command"], str):
            s["command"] = [s["command"]]
        if not isinstance(s["command"], list) or not all(isinstance(x, str) for x in s["command"]):
            raise ValueError("command должен быть строкой или списком строк")

    s["params"] = {str(k): str(v) for k, v in (s.get("params") or {}).items()}
    s["env"] = {str(k): str(v) for k, v in (s.get("env") or {}).items()}
    s["task_param"] = str(s.get("task_param") or "task")
    s["project_param"] = str(s.get("project_param") or "project_path")
    s["timeout_s"] = int(s.get("timeout_s") or 900)
    s["max_turns"] = int(s.get("max_turns") or 0)
    s["output"] = str(s.get("output") or "json")
    s["tags"] = [str(x) for x in (s.get("tags") or [])]
    s["session"] = bool(s.get("session", False))
    s["pass_full_task"] = bool(s.get("pass_full_task", False))
    s["cwd"] = str(s.get("cwd") or "")
    return s


def _fill(value: str, ctx: dict[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in ctx:
            return ctx[key]
        raise ValueError("неизвестная подстановка в спеке: {" + key + "}")
    return re.sub(r"\{([A-Za-z0-9_.\-]+)\}", sub, value)


def build(spec: dict[str, Any], *, task: str, project: str, task_file: Path,
          run_id: str) -> tuple[list[str], bool, str, dict[str, str]]:
    """Вернуть (argv, use_shell, cwd, env)."""
    ctx = {"task": task, "task_file": str(task_file), "project": project or "",
           "spec_dir": spec["base_dir"], "name": spec["name"], "run_id": run_id}
    ctx.update({k: v for k, v in spec["params"].items() if k not in ctx})

    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                "AGENT_TASK_FILE": str(task_file), "AGENT_TASK": task,
                "AGENT_RUN_ID": run_id, "AGENT_PROJECT": project or ""})
    env.update({k: _fill(v, ctx) for k, v in spec["env"].items()})

    cwd = spec["cwd"] or spec["base_dir"]
    cwd = str((Path(spec["base_dir"]) / cwd).resolve()) if not Path(cwd).is_absolute() else cwd

    if spec["runtime"] == "shell":
        argv = [_fill(x, ctx) for x in spec["command"]]
        use_shell = len(argv) == 1
        if use_shell:
            argv = [argv[0]]
        return argv, use_shell, cwd, env

    argv = ["goose", "run", "-q", "--output-format", "json"]
    if spec["runtime"] == "goose_recipe":
        argv += ["--recipe", spec["recipe"]]
        for key, val in spec["params"].items():
            argv += ["--params", key + "=" + _fill(val, ctx)]
        short = " ".join(task.strip().splitlines()[:1])[:160] if not spec["pass_full_task"] else task
        argv += ["--params", spec["task_param"] + "=" + short,
                 "--params", "task_file=" + str(task_file)]
        if project:
            argv += ["--params", spec["project_param"] + "=" + project]
    else:
        body = str(spec["instructions"]).rstrip() + "\n\n# ЗАДАЧА\n\n" + task.strip() + "\n"
        body_file = task_file.with_name(task_file.stem + ".instructions.md")
        body_file.write_text(body, encoding="utf-8", newline="\n")
        argv += ["-i", str(body_file)]
        env["AGENT_INSTRUCTIONS_FILE"] = str(body_file)
    if not spec["session"]:
        argv += ["--no-session"]
    if spec["max_turns"]:
        argv += ["--max-turns", str(spec["max_turns"])]
    return argv, False, cwd, env


def parse_output(spec: dict[str, Any], out: str) -> Any:
    text = (out or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict) and "messages" in data:
        last = ""
        for msg in data.get("messages") or []:
            if msg.get("role") == "assistant":
                for c in msg.get("content") or []:
                    if c.get("type") == "text" and c.get("text"):
                        last = c["text"]
        tokens = int((data.get("metadata") or {}).get("total_tokens") or 0)
        return {"text": last, "tokens": tokens, "status": data.get("status") or "",
                "raw": data}
    if spec["output"] == "json":
        dec = json.JSONDecoder()
        pos = 0
        while True:
            i = text.find("{")
            if i < 0:
                break
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return {"text": text, "tokens": 0, "status": "", "data": obj}
            except Exception:
                pass
            pos = i + 1
            text = text[pos:]
    return {"text": text, "tokens": 0, "status": "", "data": None}


def extract_tokens(spec: dict[str, Any], parsed: Any, out: str) -> int:
    if isinstance(parsed, dict):
        if parsed.get("tokens"):
            return int(parsed["tokens"])
        nested = parsed.get("data") or {}
        if isinstance(nested, dict) and isinstance(nested.get("tokens"), int):
            return int(nested["tokens"])
    regex = spec.get("tokens_regex") or r"(\d+)\s+токенов"
    m = re.search(regex, out or "")
    return int(m.group(1)) if m else 0

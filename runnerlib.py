"""Общий слой запуска фоновых процессов.

Два режима:
1. detach  — DETACHED_PROCESS (+ попытка выйти из job object). Быстро.
   Переживает обычное закрытие, НО клиент, который убивает дерево процессов
   (taskkill /T, Job Object), убьёт и job.
2. schtasks — задача Планировщика Windows. Процесс рождается вне дерева
   клиента, поэтому переживает всё: закрытие MCP, закрытие Desktop, Ctrl+C.
   Медленнее на 1-3 с, нужен вход пользователя в систему.

Выбор: env COMPANY_MCP_SPAWN / AGENT_RUNNER_SPAWN = detach (по умолчанию)
или schtasks.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

DEVNULL = subprocess.DEVNULL


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _log(msg: str) -> None:
    path = os.environ.get("SPAWN_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def spawn_detached(argv: list[str], cwd: str, env: dict[str, str] | None = None,
                   stdout: Any = None, stderr: Any = None) -> subprocess.Popen:
    common: dict[str, Any] = dict(
        cwd=cwd, env=env, stdin=DEVNULL,
        stdout=stdout if stdout is not None else DEVNULL,
        stderr=stderr if stderr is not None else DEVNULL,
        close_fds=True)
    if os.name == "nt":
        base = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            p = subprocess.Popen(argv, creationflags=base | 0x01000000, **common)
            _log("detach+breakaway pid=" + str(p.pid) + " " + Path(argv[1]).name)
            return p
        except OSError as exc:
            _log("breakaway failed: " + str(exc)[:200] + " -> plain detached")
            p = subprocess.Popen(argv, creationflags=base, **common)
            _log("detach pid=" + str(p.pid))
            return p
    return subprocess.Popen(argv, start_new_session=True, **common)


def spawn_scheduled(argv: list[str], task_name: str, cwd: str, state_dir: Path,
                    env_overlay: dict[str, str] | None = None) -> None:
    """Запустить через Планировщик Windows (недоступно вне Windows)."""
    if os.name != "nt":
        spawn_detached(argv, cwd=cwd)
        return
    spawn_dir = Path(state_dir) / "spawn"
    spawn_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = spawn_dir / (task_name + ".cmd")
    lines = ["@echo off", 'cd /d "' + cwd + '"']
    overlay = dict(env_overlay or {})
    overlay["SCHED_TASK_NAME"] = task_name
    for k, v in overlay.items():
        if k.upper() in ("PATH", "COMSPEC", "SYSTEMROOT"):
            continue
        lines.append('set "' + k + '=' + str(v).replace('"', "") + '"')
    lines.append(subprocess.list2cmdline(argv) +
                 ' 1>>"' + str(spawn_dir / (task_name + ".out")) + '"' +
                 ' 2>&1')
    cmd_file.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    create = ["schtasks", "/create", "/tn", task_name, "/tr", '"' + str(cmd_file) + '"',
              "/sc", "once", "/st", "23:59", "/f"]
    subprocess.run(create, capture_output=True, text=True)
    subprocess.run(["schtasks", "/run", "/tn", task_name], capture_output=True, text=True)
    _log("schtasks " + task_name)


def cleanup_scheduled() -> None:
    name = os.environ.get("SCHED_TASK_NAME")
    if not name or os.name != "nt":
        return
    try:
        subprocess.run(["schtasks", "/delete", "/tn", name, "/f"],
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def python_exe() -> str:
    return sys.executable

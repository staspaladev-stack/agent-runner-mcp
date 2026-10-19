#!/usr/bin/env python3
"""Дымовой тест MCP: клиент по stdio, проверка detached-жизни job.

  uvx --with "mcp[cli]" python client_smoke.py agent_echo
  uvx --with "mcp[cli]" python client_smoke.py agent_detach
  uvx --with "mcp[cli]" python client_smoke.py company_worker <project> <run_id|"">
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

HERE = Path(__file__).resolve().parents[1]
SERVERS = {"agent": HERE / "server.py"}
SRC = None


async def call(client, name, args=None):
    r = await client.call_tool(name, args or {})
    sc = getattr(r, "structured_content", None)
    if isinstance(sc, dict):
        if set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    out = []
    for c in r.content or []:
        out.append(getattr(c, "text", str(c)))
    return out


def params(which):
    return StdioServerParameters(
        command="uvx",
        args=["--with", "fastmcp", "--with", "pyyaml", "python", str(SERVERS[which])],
        env={**os.environ,
             "AGENT_RUNNER_SPAWN": os.environ.get("AGENT_RUNNER_SPAWN", "detach"),
             "COMPANY_MCP_SPAWN": os.environ.get("COMPANY_MCP_SPAWN", "detach")},
    )


async def agent_echo():
    async with Client(params("agent")) as c:
        tools = [t.name for t in (await c.list_tools()).tools]
        res = await call(c, "run_agent", {"agent": "echo", "task": "MCP-OK", "wait_s": 20})
        print(json.dumps({"tools": tools, "run": res}, ensure_ascii=False, indent=2)[:3000])
        rid = res.get("run_id") if isinstance(res, dict) else None
        if rid:
            print(json.dumps(await call(c, "get_run", {"run_id": rid}), ensure_ascii=False)[:1500])


async def agent_detach():
    async with Client(params("agent")) as c:
        res = await call(c, "run_agent", {"agent": "echo", "task": "DETACH-OK", "wait_s": 0})
        print("started:", json.dumps(res, ensure_ascii=False))
        rid = res["run_id"]
    print("client closed, waiting 6s")
    await asyncio.sleep(6)
    async with Client(params("agent")) as c:
        print("after reconnect:", json.dumps(await call(c, "get_run", {"run_id": rid}),
                                            ensure_ascii=False)[:1500])


async def agent_slow():
    spec = {"name": "slow", "runtime": "shell",
            "command": ["cmd", "/c", "ping -n 20 127.0.0.1 >nul & echo SLOW-OK"],
            "timeout_s": 90}
    async with Client(params("agent")) as c:
        res = await call(c, "run_agent", {"spec": spec, "task": "slow", "wait_s": 0})
        print("started:", json.dumps(res, ensure_ascii=False))
        rid = res["run_id"]
    print("client closed, sleeping 30s")
    await asyncio.sleep(30)
    async with Client(params("agent")) as c:
        print("after 30s:", json.dumps(await call(c, "get_run", {"run_id": rid}),
                                      ensure_ascii=False)[:1200])


async def company_resume(project: str, session_name: str):
    async with Client(params("company")) as c:
        res = await call(c, "start_role", {
            "role": "worker", "project": project, "session": session_name,
            "task": "Ответь JSON по схеме: status ok, summary RESUMED-OK, report коротко, "
                    "files/evidence/unverified пустые, next stop. Ничего не делай.",
            "wait_s": 180})
        print(json.dumps(res, ensure_ascii=False)[:2000])


async def company_worker(project: str, run_id: str):
    async with Client(params("company")) as c:
        if run_id:
            print(json.dumps(await call(c, "get_job", {"job_id": run_id, "include_log": True}),
                             ensure_ascii=False)[:2500])
            return
        res = await call(c, "start_role", {
            "role": "worker", "project": project,
            "task": "В project_path создай файл mcp_probe.txt с одной строкой MCP-JOB-OK. "
                    "Больше ничего не делай. Ответь JSON по схеме, status ok.",
            "wait_s": 0})
        print("started:", json.dumps(res, ensure_ascii=False))
        rid = res["job_id"]
    print("client closed; polling via new connection")
    for i in range(40):
        await asyncio.sleep(6)
        async with Client(params("company")) as c:
            job = await call(c, "get_job", {"job_id": rid})
        print(i, job.get("status"), job.get("tokens") if isinstance(job, dict) else "?")
        if isinstance(job, dict) and job.get("status") in ("ok", "fail", "timeout", "cancelled"):
            print(json.dumps(job, ensure_ascii=False)[:2500])
            break


async def main():
    mode = sys.argv[1]
    if mode == "agent_echo":
        await agent_echo()
    elif mode == "agent_detach":
        await agent_detach()
    elif mode == "agent_slow":
        await agent_slow()
    elif mode == "company_resume":
        await company_resume(sys.argv[2], sys.argv[3])
    elif mode == "company_worker":
        await company_worker(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    else:
        raise SystemExit("unknown mode")


if __name__ == "__main__":
    asyncio.run(main())

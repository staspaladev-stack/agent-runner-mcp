# agent-runner-mcp

**MCP server that runs any CLI agent as a background job.**

You describe an agent in a small spec (YAML, Markdown, JSON or inline), call
`run_agent`, and get back a `run_id`. The job runs in its own process, survives
the chat, and reports status, tokens, cost, output and a log. Works with goose
recipes, plain prompts, or any command line (`claude`, `aider`, `npx`, `python`).

It is not an agent framework. It does not decide *who* to call. It is the
execution layer: queue, isolation, supervision, results.

```
chat -> run_agent(spec, task) -> [ detached runner -> CLI agent ] -> SQLite -> get_run()
```

## Why not just subagents

| | built-in one-shot subagents | agent-runner-mcp |
|---|---|---|
| Lifetime | one call, minutes | until the process ends, detached |
| After the client closes | gone | keeps running (or survives via `schtasks`) |
| Audit | none | id, status, tokens, cost, log, JSON result |
| Cancel | no | `cancel_run` |
| Reuse | no | every run is recorded, agents are saved specs |

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip install fastmcp pyyaml`
- Optional: [goose](https://block.github.io/goose/) if you want `goose_recipe` /
  `goose_instructions` runtimes. `shell` runtime needs nothing.

## Run

```bash
git clone https://github.com/staspaladev-stack/agent-runner-mcp
cd agent-runner-mcp

# quick check, no MCP client needed
uvx --with fastmcp --with pyyaml python server.py --selftest

# as an MCP server over stdio
uvx --with fastmcp --with pyyaml python server.py
```

State lives in `~/.agent_runner` (`runs.db`, `tasks/`, `logs/`, `specs/`).
Override with `AGENT_RUNNER_HOME`.

## Connect

### goose (Desktop or CLI)

Add to `config.yaml` (`~/.config/goose/config.yaml` on Linux/macOS,
`%APPDATA%\Block\goose\config\config.yaml` on Windows), inside `extensions:`:

```yaml
  agent_runner:
    enabled: true
    type: stdio
    name: agent_runner
    description: Universal background agent runner
    cmd: uvx
    args:
    - --with
    - fastmcp
    - --with
    - pyyaml
    - python
    - /absolute/path/to/agent-runner-mcp/server.py
    envs: {}
    env_keys: []
    timeout: 600
    cwd: null
    bundled: null
```

Restart goose. The tools appear in the next session.

### Claude Desktop / Cursor / any MCP client

```json
{
  "mcpServers": {
    "agent-runner": {
      "command": "uvx",
      "args": ["--with", "fastmcp", "--with", "pyyaml",
               "python", "/absolute/path/to/agent-runner-mcp/server.py"]
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `list_agents(tag?)` | saved agents (name, runtime, description, tags) |
| `describe_agent(name)` | command, params, limits |
| `validate_agent(name?, spec?, task?)` | build the argv without running |
| `save_agent(spec)` | store an agent in `~/.agent_runner/specs` |
| `delete_agent(name)` | remove a user agent |
| `run_agent(agent?, task, project?, params?, spec?, wait_s?, timeout_s?, mode?)` | start a job |
| `get_run(run_id, include_log?)` | status, result, tokens, cost, log path |
| `list_runs(limit?, status?)` | recent jobs |
| `cancel_run(run_id)` | mark cancelled and kill the process tree |
| `import_company_specs()` | optional: turn a goose "company" registry into agents |

## Agent spec

```yaml
name: my_agent                  # required
title: Human name
description: What it does
runtime: goose_recipe           # shell | goose_recipe | goose_instructions
recipe: /abs/path/worker.yaml   # for goose_recipe
instructions: |                 # for goose_instructions (or markdown body)
  You are a concise executor.
command: ["npx", "some-cli", "{task}"]   # for shell
params: {consumer: architect}
task_param: task                # goose param that receives the task
project_param: project_path     # goose param that receives the project
session: false                  # true -> do not pass --no-session
max_turns: 30
timeout_s: 900
output: json                    # json | text
tokens_regex: '(\d+)\s+tokens'
env: {GOOSE_MODE: auto}
cwd: .
tags: [demo]
```

Placeholders in `command`/`env`: `{task}`, `{task_file}`, `{project}`,
`{spec_dir}`, `{name}`, `{run_id}` and any key from `params`.
Environment always contains `AGENT_TASK`, `AGENT_TASK_FILE`, `AGENT_RUN_ID`,
`AGENT_PROJECT`.

Three ways to define an agent:

```
# 1. saved spec
save_agent({"name": "worker", "runtime": "goose_recipe",
            "recipe": "/abs/goose/roles/worker.yaml",
            "task_param": "task", "project_param": "project_path"})
run_agent(agent="worker", project="/srv/shop", task="add /health endpoint")

# 2. ad-hoc spec, no file
run_agent(spec={"name": "tmp", "runtime": "shell",
                "command": ["bash", "-lc", "echo {task}"]}, task="hello")

# 3. plain prompt (markdown with frontmatter, runtime: goose_instructions)
run_agent(agent="prompt_only", task="Explain MCP in two sentences")
```

## Statuses

`queued` → `running` → `ok` | `fail` | `timeout` | `cancelled` | `lost`

`lost` means the runner disappeared: the host client killed its process tree.
Use `mode="schtasks"` (Windows Task Scheduler) to make the job independent of
the client's process tree.

## Run modes

| | `detach` (default) | `schtasks` (Windows) |
|---|---|---|
| Start | instant | +1–3 s |
| Survives closing the MCP server | yes | yes |
| Survives `taskkill /T` from the client | no | yes |
| Requires | — | user logged in |

Per call: `run_agent(..., mode="schtasks")`. Globally:
`AGENT_RUNNER_SPAWN=schtasks` in the extension `envs`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_RUNNER_HOME` | `~/.agent_runner` | state dir (db, tasks, logs, user specs) |
| `AGENT_RUNNER_DB` | `$HOME/runs.db` | SQLite path |
| `AGENT_RUNNER_SPECS` | — | extra spec dirs, `os.pathsep` separated |
| `AGENT_RUNNER_SPAWN` | `detach` | `detach` or `schtasks` |
| `AGENT_RUNNER_MAX_CONCURRENT` | `3` | simultaneous jobs |
| `SPAWN_LOG` | `$HOME/spawn.log` | spawn diagnostics |

## Security

- A spec is code: `shell` runs whatever you put there. Treat specs as code,
  review before `save_agent`.
- `run_agent` only executes specs you provide; there is no remote fetching.
- Jobs inherit the server's environment, including API keys. Run the server
  under the account that should own those keys.
- `project` is validated as an existing directory but is not sandboxed.

## Tests

```bash
uvx --with "mcp[cli]" python tests/client_smoke.py agent_echo
uvx --with "mcp[cli]" python tests/client_smoke.py agent_detach
uvx --with "mcp[cli]" python tests/client_smoke.py agent_slow
```

## License

MIT

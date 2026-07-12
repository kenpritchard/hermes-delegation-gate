# delegation-gate

A tiny [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that requires **human approval before `delegate_task` spawns subagents** — so your agent can't silently fan out expensive parallel work.

## ⚠️ Runtime requirement (read this first)

This plugin uses the `pre_tool_call` → `action: "approve"` directive, which Hermes only honors on builds **newer than commit `36308f066`** (`feat(plugins): pass approve rule keys to approval gate`, ~2026-07-07).

**On older builds (e.g. v0.18.0), the plugin loads without error but silently does not gate anything.** Verify your runtime before relying on it:

```bash
python3 -c "import hermes_cli.plugins as p; print(hasattr(p, 'resolve_pre_tool_block'))"
# must print: True
```

(Run with the Python interpreter of your Hermes install.)

## Why

Subagent delegation is powerful but easy for the model to overuse. In the incident that motivated this plugin, an agent spawned a batch of subagents on a job that didn't need them; re-running the same job with delegation disabled produced almost as good a result **for less than half the tokens**.

Disabling the delegation toolset entirely throws away the upside. This plugin keeps delegation available but puts a human decision in front of every use — the agent proposes, you approve.

Hermes configuration allows for assigning less expensive models to subagents or limiting their number, but nothing allowed for the default configuration to be enabled with a guard that would allow for human approval before execution so this plugin was developed.

## What it does

When the model calls `delegate_task`, the call is intercepted **before execution** and escalated to the same `[o]nce / [s]ession / [a]lways / [d]eny` human-approval gate Hermes uses for dangerous shell commands. The model cannot skip or bypass it.

The approval prompt shows what you'd be paying for:

- single task: the subagent's goal
- batch fan-out: the task count and a preview of each goal (first 5, then `+N more`) — important, since one `delegate_task` call can spawn several children

Approval semantics:

- **once** — this call only; next call prompts again
- **session** — the rest of the session is approved ("yes, this job is worth parallelizing")
- **always** — opens delegation permanently for the profile. The plugin deliberately uses one stable rule key (`delegate_task:spawn-subagents`) so `always` means what it says; avoid it unless you've decided you no longer want the gate.
- **deny** — the call is blocked; the model sees the denial as the tool result

**Fail-closed:** in non-interactive contexts (cron jobs, scripts) with no human present, gated calls are **blocked**, not silently allowed. Set `approvals.cron_mode: approve` if you truly want unattended delegation — better to keep the delegation toolset off on cron profiles entirely.

## Install

```bash
hermes plugins install kenpritchard/hermes-delegation-gate --enable
```

No tool override grant is needed — the plugin registers a hook, not a tool replacement.

### Installing for a specific profile

Plugins are per-profile: each profile has its own `plugins/` directory and its own `plugins.enabled` allow-list in `config.yaml`, so you can gate one profile and leave others untouched.

`hermes plugins install` has no profile flag of its own — use the **global** `-p` flag, which repoints the Hermes home (and therefore the plugins directory) at that profile:

```bash
hermes -p myprofile plugins install kenpritchard/hermes-delegation-gate --enable
hermes -p myprofile plugins list      # verify: delegation-gate enabled
```

Or manually: copy `plugin.yaml` and `__init__.py` into the profile's plugins directory —

```
<hermes home>/profiles/<name>/plugins/delegation-gate/   # named profile
~/.hermes/plugins/delegation-gate/                       # default profile
```

— then `hermes -p <name> plugins enable delegation-gate`. (Git install is preferred: it lets `hermes plugins update delegation-gate` pull future versions, which a hand-copied directory can't.)

Note the delegation **toolset** is a separate per-profile switch from the plugin: the plugin gates `delegate_task` only where the tool is enabled at all (`hermes tools enable delegation -p <name>`). A sensible fleet setup is: toolset off on unattended/cron profiles, toolset on + this gate everywhere else.

### Verify it gates

Start an interactive session and ask the agent to delegate something trivial — you should get the `once/session/always/deny` prompt with a goal preview before any subagent starts. If delegation runs without a prompt, re-check the runtime requirement at the top of this README.

## Companion cost controls

The gate decides *whether* delegation runs; these `config.yaml` settings limit the blast radius *when you approve*:

```yaml
delegation:
  max_concurrent_children: 2   # default 3
  max_iterations: 20           # tool-call cap per subagent (default 50)
  max_spawn_depth: 1           # no grandchildren
  child_timeout_seconds: 600   # wall-clock cap (default unlimited)
  reasoning_effort: low        # don't inherit an expensive parent level
  # model: <a cheap model>     # route subagents to a cheaper model
```

Prompt-level guidance helps too — a line in `SOUL.md`/`AGENTS.md` that delegation is expensive and should only be proposed for genuine parallelism or context isolation reduces how often you're even prompted.

## How it works

`agent/tool_executor.py` → `hermes_cli/plugins.py:resolve_pre_tool_block()` → `tools/approval.py:request_tool_approval()`. The hook returns:

```python
{"action": "approve", "message": "<what's being requested>", "rule_key": "delegate_task:spawn-subagents"}
```

Approval outcomes reuse Hermes' existing session/permanent allowlist machinery; a gate error fails closed to a block.

## License

MIT

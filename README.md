# delegation-gate

A tiny [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that requires **human approval before `delegate_task` spawns subagents** — so your agent can't silently fan out expensive parallel work.

## ⚠️ Runtime requirement (read this first)

The default **approve mode** uses the `pre_tool_call` → `action: "approve"` directive, which Hermes only honors on builds **newer than commit `36308f066`** (`feat(plugins): pass approve rule keys to approval gate`, ~2026-07-07).

**On older builds (e.g. v0.18.0), approve mode loads without error but silently does not gate anything.** Verify your runtime before relying on it:

```bash
python3 -c "import hermes_cli.plugins as p; print(hasattr(p, 'resolve_pre_tool_block'))"
# must print: True
```

(Run with the Python interpreter of your Hermes install.)

If it prints `False`, you can't use approve mode — but **[block mode](#block-mode-delegation_gate_mode)** (`DELEGATION_GATE_MODE=block`) still works: the `block` directive predates the approve action, so on older runtimes the plugin can refuse delegation outright even though it can't prompt.

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

**YOLO mode bypasses this gate.** Like every approval prompt in Hermes, running with `--yolo` (or the session `/yolo` toggle) skips the gate entirely and delegation proceeds unprompted. That's by design — yolo means "I trust the agent with everything" — but if uncontrolled delegation spend is the very thing you're guarding against, don't run gated profiles in yolo mode.

## Install

```bash
hermes plugins install kenpritchard/hermes-delegation-gate --enable
```

When enabling, Hermes asks *"Allow this plugin to replace built-in tools (e.g. shell_exec, write_file)?"* — it asks this for every plugin. **Answer No**: delegation-gate registers a hook, not a tool replacement, and works fully without the grant. To skip the prompt (e.g. in scripts):

```bash
hermes plugins enable delegation-gate --no-allow-tool-override
```

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

— then `hermes -p <name> plugins enable delegation-gate --no-allow-tool-override`. (Git install is preferred: it lets `hermes plugins update delegation-gate` pull future versions, which a hand-copied directory can't.)

Note the delegation **toolset** is a separate per-profile switch from the plugin: the plugin gates `delegate_task` only where the tool is enabled at all (`hermes tools enable delegation -p <name>`). A sensible fleet setup is: toolset off on unattended/cron profiles, toolset on + this gate everywhere else.

### Verify it gates

Start an interactive session and give the agent a prompt that forces a delegation:

```
Use delegate_task to list the contents of this directory and return the count. Do not do it yourself.
```

You should get the `once/session/always/deny` prompt with a goal preview before any subagent starts (or, in block mode, an immediate refusal). If delegation runs without a prompt, re-check the runtime requirement at the top of this README.

## Block mode (`DELEGATION_GATE_MODE`)

By default the gate *asks* (`approve` mode). Set `DELEGATION_GATE_MODE=block` to *refuse* delegation outright: the model gets a policy message as the tool result ("delegation is disabled by policy... complete the task yourself") and adapts, with no prompt and no waiting.

Block mode differs from approve mode in two ways that matter:

- **It holds under yolo mode.** `--yolo` / `/yolo` bypasses approval prompts (including this plugin's approve mode), but a `block` directive never reaches the approval machinery — there is nothing to bypass.
- **It works on older runtimes.** Hermes builds that predate the `approve` action (see the runtime requirement above) still honor `block`, so block mode functions even where approve mode silently doesn't.

Ways to set it, from least to most persistent:

#### macOS / Linux

The `VAR=value command` syntax (env var prefix before the command) sets the variable for that single process only — it doesn't pollute the shell afterward.

```bash
# one run only (shell env prefix — before the command, not after)
DELEGATION_GATE_MODE=block hermes -p batch-profile

# yolo run with delegation still locked down: yolo skips every approval
# prompt, but block never reaches the approval machinery, so the agent
# runs unattended EXCEPT it cannot delegate
DELEGATION_GATE_MODE=block hermes -p batch-profile --yolo

# per profile, persistent: add to the profile's .env file
echo 'DELEGATION_GATE_MODE=block' >> <hermes home>/profiles/batch-profile/.env

# machine-wide: export from your shell profile (rarely what you want)
```

#### Windows

The `VAR=value command` syntax does **not** work in cmd.exe or PowerShell — Windows shells don't support prepending an env var assignment to a command. The closest one-liners set the variable in the current shell, which means it lingers for that session (not truly one-run-only):

**PowerShell** (sets it for the current shell session; it persists after the command finishes):

```powershell
$env:DELEGATION_GATE_MODE='block'; hermes -p batch-profile
```

**cmd.exe** (same caveat — `set` persists for the shell session):

```cmd
set DELEGATION_GATE_MODE=block && hermes -p batch-profile
```

If you need a true one-run-only setting on Windows, use the `.env` route instead — it scopes the variable to the profile without polluting any shell session:

```powershell
Add-Content <hermes home>\profiles\batch-profile\.env 'DELEGATION_GATE_MODE=block'
```

The mode is read **once at plugin load** (mirroring how Hermes freezes yolo mode at import): it's a policy set by whoever launched the process, not something a session can flip. Unset or `approve` = prompt; any unrecognized value logs a warning and falls back to `block`, so a typo on an unattended profile fails safe rather than open.

Suggested fleet pattern: approve mode (default) on interactive profiles, `block` in the `.env` of cron/unattended/yolo profiles.

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

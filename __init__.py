"""delegation-gate: human approval required before delegate_task spawns subagents.

Returning {"action": "approve", ...} from pre_tool_call escalates the call to
the same [o]nce/[s]ession/[a]lways/[d]eny gate that dangerous shell commands
use (see tools/approval.py:request_tool_approval). Cron and other
non-interactive contexts fail closed unless approvals.cron_mode allows it.

Live-control calls (action list/steer/stop on running children) are never
gated: they cannot spawn anything, and stopping a child should never need
permission.

Set DELEGATION_GATE_MODE=block to refuse delegation outright instead of
prompting: no approval gate, so it also holds under yolo mode and on older
Hermes runtimes that ignore the "approve" action. Unset or "approve" = prompt.
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

GATED_TOOLS = {"delegate_task"}

# delegate_task's live-control actions (Hermes v0.21+) act on children that are
# already running: list them, steer them, stop them. They never spawn anything
# (Hermes ignores goal/tasks for them), so they pass through ungated in both
# modes -- prompting before "stop" would make a cost-control plugin get in the
# way of cutting cost. Normalized exactly like tools/delegate_tool.py does.
CONTROL_ACTIONS = frozenset({"list", "steer", "stop"})

# Policy is read once per process and then frozen, mirroring Hermes' own yolo
# handling: the mode is set by whoever launched the process (shell prefix or
# the profile's .env), not something mutable from inside a session.
#
# It is read on the first gated call, not at import: Hermes discovers and
# imports plugins *before* it loads the profile's .env (seen on v0.23.0), so a
# value read at import would never see DELEGATION_GATE_MODE set in .env. By the
# time the agent can call delegate_task, .env has been loaded.
#
# The process environment alone isn't reliable either: on a v0.23 gateway the
# hook can run where the profile's .env never reached os.environ. So the
# profile's own .env file is read directly as well, and "block" from either
# place wins (fail safe).
#
# One gateway process can serve several profiles (gateway.multiplex_profiles),
# so the mode is remembered per profile home, never once for the whole process.
_SETTING = "DELEGATION_GATE_MODE"
_VALID_MODES = ("approve", "block")
_MODES: dict[str, str] = {}


def _profile_home() -> str:
    try:
        from hermes_constants import get_hermes_home  # honors the per-profile home override
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME", "")


def _profile_env_file() -> Path | None:
    home = _profile_home()
    return Path(home) / ".env" if home else None


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        from agent.secret_scope import load_env_file  # Hermes' own .env tokenizer
        return load_env_file(path)
    except ImportError:
        pass
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return values


def _settings() -> list[tuple[str, str]]:
    """Every place the setting is defined, as (source, raw value)."""
    found = []
    path = _profile_env_file()
    if path is not None and path.is_file():
        value = _read_env_file(path).get(_SETTING)
        if value is not None:
            found.append((str(path), value))
    if os.environ.get(_SETTING) is not None:
        found.append(("environment", os.environ[_SETTING]))
    return found


def _resolve_mode() -> tuple[str, str]:
    """(mode, where it came from). Unset everywhere = approve; block or a typo anywhere = block."""
    found = _settings()
    if not found:
        return "approve", "default"
    for source, raw in found:
        value = raw.strip().lower() or "approve"
        if value == "block":
            return "block", source
        if value not in _VALID_MODES:
            # Unrecognized value: fall back to "block", not "approve" — a
            # misconfigured unattended profile must not run with an open gate.
            logger.warning(
                "[delegation-gate] unrecognized %s=%r in %s; falling back to 'block' (valid values: %s)",
                _SETTING, raw, source, ", ".join(_VALID_MODES),
            )
            return "block", source
    return "approve", found[0][0]


def _mode() -> str:
    """The profile's gate mode: read on first use, then fixed for the life of the process."""
    home = _profile_home()
    mode = _MODES.get(home)
    if mode is None:
        mode, source = _resolve_mode()
        _MODES[home] = mode
        logger.info("[delegation-gate] mode=%s (from %s; profile %s; pid %s)",
                    mode, source, home or "?", os.getpid())
    return mode

BLOCK_MESSAGE = (
    "Delegation is disabled by policy on this profile "
    "(DELEGATION_GATE_MODE=block). Do not retry delegate_task. "
    "Complete the task yourself without spawning subagents."
)

# One stable rule key: answering [a]lways opens delegation permanently for
# this profile, regardless of goal text. Deliberate — the per-reason default
# would create a new allowlist entry per goal, making [a]lways meaningless.
RULE_KEY = "delegate_task:spawn-subagents"

_PREVIEW_CHARS = 160
_BATCH_PREVIEW_LIMIT = 5


def _preview_goal(text, limit: int = _PREVIEW_CHARS) -> str:
    logger.debug("previewing goal text: %r", text)
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _summarize(args: dict) -> str:
    tasks = args.get("tasks")
    if isinstance(tasks, list) and tasks:
        previews = []
        for i, item in enumerate(tasks[:_BATCH_PREVIEW_LIMIT], start=1):
            if isinstance(item, dict):
                goal = _preview_goal(item.get("goal"))
            else:
                goal = _preview_goal(item)
            previews.append(f"{i}. {goal or '(no goal)'}")
        more = ""
        if len(tasks) > _BATCH_PREVIEW_LIMIT:
            more = f" (+{len(tasks) - _BATCH_PREVIEW_LIMIT} more)"
        joined = " | ".join(previews) if previews else "(no goals)"
        return f"batch fan-out of {len(tasks)} subagent task(s){more}: {joined}"

    goal = _preview_goal(args.get("goal"))
    return f"1 subagent, goal: {goal or '(no goal given)'}"


def _is_control_action(args: dict) -> bool:
    action = args.get("action")
    return isinstance(action, str) and action.strip().lower() in CONTROL_ACTIONS


def _pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    logger.debug("[delegation-gate] pre_tool_call: %s, args=%s", tool_name, args)
    if tool_name not in GATED_TOOLS:
        return None
    args = args if isinstance(args, dict) else {}
    if _is_control_action(args):
        return None
    if _mode() == "block":
        return {"action": "block", "message": BLOCK_MESSAGE}
    summary = _summarize(args)
    logger.debug("[delegation-gate] summary=%s", summary)
    return {
        "action": "approve",
        "message": (
            "Delegation costs extra tokens — confirm this is worth "
            f"parallelizing. Requested: {summary}"
        ),
        "rule_key": RULE_KEY,
    }


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)

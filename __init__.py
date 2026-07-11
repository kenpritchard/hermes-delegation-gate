"""delegation-gate: human approval required before delegate_task spawns subagents.

Returning {"action": "approve", ...} from pre_tool_call escalates the call to
the same [o]nce/[s]ession/[a]lways/[d]eny gate that dangerous shell commands
use (see tools/approval.py:request_tool_approval). Cron and other
non-interactive contexts fail closed unless approvals.cron_mode allows it.
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

GATED_TOOLS = {"delegate_task"}

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


def _pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    logger.debug("[delegation-gate] pre_tool_call: %s, args=%s", tool_name, args)
    if tool_name not in GATED_TOOLS:
        return None
    summary = _summarize(args if isinstance(args, dict) else {})
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

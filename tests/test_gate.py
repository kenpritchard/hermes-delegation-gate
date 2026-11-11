"""Unit tests for delegation-gate. Run: python -m pytest tests/ (no Hermes needed)."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(monkeypatch, mode=None):
    """Import a fresh copy, like the real plugin load. The mode is read on the first gated call."""
    if mode is None:
        monkeypatch.delenv("DELEGATION_GATE_MODE", raising=False)
    else:
        monkeypatch.setenv("DELEGATION_GATE_MODE", mode)
    name = f"delegation_gate_under_test_{mode}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(monkeypatch):
    return load(monkeypatch)


def test_other_tools_pass(gate):
    assert gate._pre_tool_call("terminal", {"command": "ls"}) is None


def test_single_spawn_asks_with_goal_and_stable_rule_key(gate):
    r = gate._pre_tool_call("delegate_task", {"goal": "list   /tmp\nand count"})
    assert r["action"] == "approve"
    assert "1 subagent, goal: list /tmp and count" in r["message"]
    assert r["rule_key"] == "delegate_task:spawn-subagents"


def test_explicit_spawn_action_is_gated(gate):
    assert gate._pre_tool_call("delegate_task", {"action": "spawn", "goal": "x"})["action"] == "approve"
    assert gate._pre_tool_call("delegate_task", {"action": " SPAWN ", "goal": "x"})["action"] == "approve"


def test_batch_preview_is_numbered_and_capped(gate):
    tasks = [{"goal": f"task {i}"} for i in range(1, 8)]
    msg = gate._pre_tool_call("delegate_task", {"tasks": tasks})["message"]
    assert "batch fan-out of 7 subagent task(s) (+2 more)" in msg
    assert "5. task 5" in msg and "task 6" not in msg


@pytest.mark.parametrize("action", ["list", "steer", "stop", "STOP", " stop "])
def test_control_actions_pass_through(gate, action):
    assert gate._pre_tool_call("delegate_task", {"action": action, "subagent_id": "sa-1"}) is None


def test_control_action_with_smuggled_goal_still_passes(gate):
    # Hermes ignores goal/tasks for control actions, so nothing can be spawned this way.
    assert gate._pre_tool_call("delegate_task", {"action": "list", "goal": "spawn me"}) is None


@pytest.mark.parametrize("action", ["", None, 5, "bogus"])
def test_anything_not_a_control_action_is_gated(gate, action):
    # Unknown actions are rejected by Hermes anyway; the gate errs toward asking.
    assert gate._pre_tool_call("delegate_task", {"action": action, "goal": "x"})["action"] == "approve"


def test_bad_args_shape_still_gated(gate):
    assert gate._pre_tool_call("delegate_task", None)["action"] == "approve"
    assert gate._pre_tool_call("delegate_task", "nonsense")["action"] == "approve"


def test_block_mode_blocks_spawns_but_not_control(monkeypatch):
    g = load(monkeypatch, "block")
    assert g._pre_tool_call("delegate_task", {"goal": "x"}) == {"action": "block", "message": g.BLOCK_MESSAGE}
    assert g._pre_tool_call("delegate_task", {"action": "stop", "subagent_id": "sa-1"}) is None


def test_unknown_mode_fails_safe_to_block(monkeypatch):
    assert load(monkeypatch, "yes-please")._mode() == "block"


@pytest.mark.parametrize("raw", ["approve", "APPROVE", "  "])
def test_approve_mode_spellings(monkeypatch, raw):
    assert load(monkeypatch, raw)._mode() == "approve"


def test_mode_set_after_import_is_honored(monkeypatch):
    # Hermes v0.23 imports plugins before it loads the profile's .env, so the
    # variable appears only after import. The first gated call must see it.
    g = load(monkeypatch)                      # imported with the variable unset
    monkeypatch.setenv("DELEGATION_GATE_MODE", "block")   # then .env is loaded
    assert g._pre_tool_call("delegate_task", {"goal": "x"})["action"] == "block"


def test_mode_is_fixed_after_first_gated_call(monkeypatch):
    # Read once, then frozen: changing the variable mid-session does nothing.
    g = load(monkeypatch, "block")
    assert g._pre_tool_call("delegate_task", {"goal": "x"})["action"] == "block"
    monkeypatch.setenv("DELEGATION_GATE_MODE", "approve")
    assert g._pre_tool_call("delegate_task", {"goal": "x"})["action"] == "block"


def test_other_tools_and_control_actions_do_not_fix_the_mode(monkeypatch):
    # Only a gated spawn reads the mode, so early non-gated calls can't
    # freeze it before .env is loaded.
    g = load(monkeypatch)
    g._pre_tool_call("terminal", {"command": "ls"})
    g._pre_tool_call("delegate_task", {"action": "list"})
    monkeypatch.setenv("DELEGATION_GATE_MODE", "block")
    assert g._pre_tool_call("delegate_task", {"goal": "x"})["action"] == "block"


def test_register_declares_only_pre_tool_call(gate):
    seen = []

    class Ctx:
        def register_hook(self, name, fn):
            seen.append(name)

    gate.register(Ctx())
    assert seen == ["pre_tool_call"]

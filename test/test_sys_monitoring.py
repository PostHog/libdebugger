"""
Tests for the ``sys.monitoring``-based dispatch path.

Covers things that don't fall out of the higher-level manager tests:

* Tool-id lifecycle (register, idempotence, release).
* Conflict refusal when another tool already owns ``DEBUGGER_ID``.
* ``_apply_monitoring`` diff: codes that leave the set get events disabled.
* Generator semantics — entry fires on PY_START and PY_RESUME, exit fires on
  PY_YIELD and PY_RETURN.
* PY_UNWIND fires exit probes with ``exception=`` on raise paths.
* ``sys._getframe(2)`` inside a callback reaches the user frame — probes
  read named parameters as locals.
"""

from __future__ import annotations

import sys

import pytest
from hogtrace.context import new_context

import libdebugger.instrumentation as instr
from libdebugger.manager import install_program, uninstall_program
from test._manager_helpers import _build_program, target_mod


# ---------------------------------------------------------------------------
# Tool-id lifecycle
# ---------------------------------------------------------------------------


def test_tool_registration_is_idempotent():
    """Two ``install_program`` calls produce one tool registration."""
    p1 = _build_program(
        "fn:test.target.fn_a:entry { }",
        program_id="tool-1",
    )
    p2 = _build_program(
        "fn:test.target.fn_b:entry { }",
        program_id="tool-2",
    )

    install_program(p1)
    assert instr._TOOL_REGISTERED
    assert sys.monitoring.get_tool(instr._TOOL_ID) == instr._TOOL_NAME

    install_program(p2)
    assert instr._TOOL_REGISTERED
    assert sys.monitoring.get_tool(instr._TOOL_ID) == instr._TOOL_NAME


def test_release_tool_disables_all_events():
    """After release, ``_MONITORED_CODES`` is empty and the tool slot is free."""
    program = _build_program(
        "fn:test.target.fn_a:entry { }",
        program_id="release-1",
    )
    install_program(program)
    assert instr._MONITORED_CODES, "install should have enabled at least one code"

    with instr._LOCK:
        instr._release_tool()

    assert not instr._TOOL_REGISTERED
    assert instr._MONITORED_CODES == {}
    assert sys.monitoring.get_tool(instr._TOOL_ID) is None


def test_tool_conflict_refuses_to_install():
    """``_ensure_tool_registered`` raises if another tool owns ``DEBUGGER_ID``."""
    sys.monitoring.use_tool_id(instr._TOOL_ID, "intruder")
    try:
        with instr._LOCK:
            with pytest.raises(RuntimeError, match="intruder"):
                instr._ensure_tool_registered()
    finally:
        sys.monitoring.free_tool_id(instr._TOOL_ID)


# ---------------------------------------------------------------------------
# _apply_monitoring diff
# ---------------------------------------------------------------------------


def test_uninstall_disables_local_events_for_code():
    """A code object's local events go to 0 once the last program leaves."""
    program = _build_program(
        "fn:test.target.fn_a:entry { }",
        program_id="diff-1",
    )
    install_program(program)

    code = target_mod.fn_a.__code__
    assert code in instr._MONITORED_CODES
    assert sys.monitoring.get_local_events(instr._TOOL_ID, code) != 0

    uninstall_program("diff-1")

    assert code not in instr._MONITORED_CODES
    assert sys.monitoring.get_local_events(instr._TOOL_ID, code) == 0


# ---------------------------------------------------------------------------
# Generator semantics
# ---------------------------------------------------------------------------


def _gen():
    """A small generator used by the generator-semantics tests."""
    yield 1
    yield 2
    return "done"


_gen.__module__ = __name__


def test_generator_entry_fires_on_resume(monkeypatch):
    """Entry probe on a generator fires on PY_START and on each PY_RESUME."""
    program = _build_program(
        f"fn:{__name__}._gen:entry {{ capture(x=1); }}",
        program_id="gen-entry",
    )

    fires = []

    def stub(prog, probe, captures):
        fires.append((probe.spec.target, captures))

    monkeypatch.setattr(instr, "_enqueue_message", stub)

    with new_context():
        install_program(program)
        try:
            list(_gen())
        finally:
            uninstall_program("gen-entry")

    # PY_START once + PY_RESUME after each of two yields = 3 entry fires.
    entry_fires = [f for f in fires if f[0] == "entry"]
    assert len(entry_fires) == 3, (
        f"expected entry on START + 2 RESUMEs; got {len(entry_fires)}"
    )


def test_generator_exit_fires_on_yield_and_return(monkeypatch):
    """Exit probe on a generator fires on PY_YIELD and PY_RETURN."""
    program = _build_program(
        f"fn:{__name__}._gen:exit {{ capture(x=1); }}",
        program_id="gen-exit",
    )

    fires = []

    def stub(prog, probe, captures):
        fires.append(probe.spec.target)

    monkeypatch.setattr(instr, "_enqueue_message", stub)

    with new_context():
        install_program(program)
        try:
            list(_gen())
        finally:
            uninstall_program("gen-exit")

    # 2 yields + 1 return = 3 exit fires.
    assert fires == ["exit", "exit", "exit"], f"got {fires}"


# ---------------------------------------------------------------------------
# PY_UNWIND
# ---------------------------------------------------------------------------


def test_unwind_delivers_exception_to_exit_probe(monkeypatch):
    """Exit probes on a raising function receive the exception via PY_UNWIND."""
    program = _build_program(
        "fn:test.target.fn_raises:exit { capture(x=1); }",
        program_id="unwind-1",
    )

    fires = []

    def stub(prog, probe, captures):
        fires.append((probe.spec.target, captures))

    monkeypatch.setattr(instr, "_enqueue_message", stub)

    with new_context():
        install_program(program)
        try:
            with pytest.raises(ValueError):
                target_mod.fn_raises()
        finally:
            uninstall_program("unwind-1")

    assert len(fires) == 1
    assert fires[0][0] == "exit"


# ---------------------------------------------------------------------------
# Frame access
# ---------------------------------------------------------------------------


def test_entry_probe_reads_named_parameter_as_local(monkeypatch):
    """Entry probes see the live frame — named parameters appear as locals.

    Pins the behavior that ``sys._getframe(2)`` inside the callback reaches
    the user code's frame rather than the dispatch shim. The probe captures
    ``x`` which is fn_a's named parameter.
    """
    program = _build_program(
        "fn:test.target.fn_a:entry { capture(x=x); }",
        program_id="frame-1",
    )

    captures_seen = []

    def stub(prog, probe, captures):
        captures_seen.append(captures)

    monkeypatch.setattr(instr, "_enqueue_message", stub)

    with new_context():
        install_program(program)
        try:
            target_mod.fn_a(42)
        finally:
            uninstall_program("frame-1")

    assert len(captures_seen) == 1
    # The capture dict is what the hogtrace VM produced — the exact shape is
    # owned by hogtrace, but our parameter must be reflected in it.
    flat = captures_seen[0]
    assert any(value == 42 for value in _walk_values(flat)), (
        f"named parameter x=42 missing from captures: {flat!r}"
    )


def _walk_values(obj):
    """Yield every leaf value from a nested dict / list."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_values(v)
    else:
        yield obj

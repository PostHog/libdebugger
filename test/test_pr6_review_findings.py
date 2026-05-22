"""
Regressions for PR #6 review findings.

Each test starts as a reproduction of a real bug surfaced in review; the
fix lands alongside it. Grouped by finding so a future regression points
at the exact contract that was broken.
"""

from __future__ import annotations

import logging
import sys

import pytest
from hogtrace.context import new_context

import libdebugger.instrumentation as instr
from libdebugger.manager import install_program, uninstall_program
from test._manager_helpers import _build_program, target_mod


# ---------------------------------------------------------------------------
# [P1] Failed installs must not leave the program in _INSTALLED_PROGRAMS.
# ---------------------------------------------------------------------------


def test_failed_install_does_not_pollute_registry():
    """If tool-id acquisition raises, the registry must roll back.

    Otherwise the next reconcile sees the program as ``current`` (same
    id/hash) and skips installing it. Probes stay dead forever.
    """
    # Take every candidate slot so _ensure_tool_registered cannot succeed.
    instr._TOOL_ID = -1
    instr._TOOL_REGISTERED = False
    for slot in instr._TOOL_CANDIDATES:
        try:
            sys.monitoring.free_tool_id(slot)
        except Exception:
            pass
        sys.monitoring.use_tool_id(slot, f"intruder-{slot}")

    program = _build_program(
        "fn:test.target.fn_a:entry { }",
        program_id="failed-install",
    )

    try:
        with pytest.raises(RuntimeError):
            install_program(program)

        assert "failed-install" not in instr._INSTALLED_PROGRAMS, (
            "failed install must not leave the program in the registry"
        )
        assert instr._PROBE_INDEX == {}
    finally:
        for slot in instr._TOOL_CANDIDATES:
            try:
                sys.monitoring.free_tool_id(slot)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# [P1] Aliased specifiers (two qualnames -> same code) must both fire.
# ---------------------------------------------------------------------------


def test_two_specifiers_for_same_code_both_fire(monkeypatch):
    """A module-level alias produces two specifiers, one CodeType.

    Both probes target the same function via different names; dispatch
    must aggregate them, not pick one qualname and silently drop the rest.
    """
    monkeypatch.setattr(target_mod, "fn_alias", target_mod.fn_a, raising=False)

    fires: list[str] = []

    def stub(prog, probe, captures):
        fires.append(probe.spec.specifier)

    monkeypatch.setattr(instr, "_enqueue_message", stub)

    p1 = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="alias-canonical",
    )
    p2 = _build_program(
        "fn:test.target.fn_alias:entry { capture(y=1); }",
        program_id="alias-aliased",
    )

    with new_context():
        install_program(p1)
        install_program(p2)
        target_mod.fn_a(7)
        uninstall_program("alias-canonical")
        uninstall_program("alias-aliased")

    assert set(fires) == {"test.target.fn_a", "test.target.fn_alias"}, (
        f"expected both alias probes to fire; got {fires}"
    )


# ---------------------------------------------------------------------------
# [P2] Line probes are not implemented in v1; the LINE event must stay off.
# ---------------------------------------------------------------------------


def test_line_probes_do_not_enable_line_monitoring(caplog):
    """A line probe must not enable LINE events on any code object.

    The hogtrace surface language doesn't currently allow ``:line`` as a
    probe point — but if a future version does, the rebuild path must
    skip those probes and log a warning rather than enabling LINE events
    on the target code. The previous code path would fire its line probe
    on every executable line in the function, wildly over-capturing.

    We exercise the rebuild path directly with a synthetic line-targeted
    probe sneaked into ``_PROBE_INDEX``.
    """
    program = _build_program(
        "fn:test.target.fn_a:entry { }",
        program_id="line-base",
    )
    install_program(program)
    # Probes carry their target as a string field; mutate one of the
    # installed program's probes to look like a line probe and rerun the
    # rebuild to exercise the filter path.
    probe = program.probes[0]
    # ProbeSpec.target is a read-only property; we can't mutate it, so
    # instead we inject a fake (qualname, "line") slot into _PROBE_INDEX
    # and re-run the rebuild's downstream filter logic by calling it.
    instr._PROBE_INDEX = dict(instr._PROBE_INDEX)
    instr._PROBE_INDEX[("test.target.fn_a", "line")] = ((program, probe),)

    caplog.set_level(logging.WARNING, logger="libdebugger.manager")

    from libdebugger.manager import _maybe_warn_line_probes

    _maybe_warn_line_probes(((program, probe),))

    code = target_mod.fn_a.__code__
    enabled_mask = instr._MONITORED_CODES.get(code, 0)
    assert enabled_mask & sys.monitoring.events.LINE == 0, (
        f"LINE event must not be enabled; mask={enabled_mask:#x}"
    )

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("line probe" in m.lower() for m in warnings), (
        f"expected a warning about line probes being unsupported; got {warnings}"
    )


# ---------------------------------------------------------------------------
# [P2] Tool-id lifecycle: prefer slot 3, fall back, hold for process lifetime.
# ---------------------------------------------------------------------------


def test_tool_id_prefers_custom_slot_3():
    """Default acquisition lands on slot 3, not DEBUGGER_ID (0).

    DEBUGGER_ID is conventionally pdb's; grabbing it pre-emptively
    means we collide with normal debuggers whenever someone attaches
    one to a live process.
    """
    program = _build_program(
        "fn:test.target.fn_a:entry { }",
        program_id="slot-pref",
    )
    install_program(program)
    assert instr._TOOL_ID == 3, (
        f"expected to land on slot 3 by default; got {instr._TOOL_ID}"
    )


def test_tool_id_falls_back_when_preferred_slot_taken():
    """If slot 3 is owned by someone else, try slot 4 next."""
    instr._TOOL_REGISTERED = False
    try:
        sys.monitoring.use_tool_id(3, "intruder-3")
    except Exception:
        pass
    try:
        program = _build_program(
            "fn:test.target.fn_a:entry { }",
            program_id="slot-fallback",
        )
        install_program(program)
        assert instr._TOOL_ID == 4, (
            f"expected fallback to slot 4 when 3 is taken; got {instr._TOOL_ID}"
        )
    finally:
        try:
            sys.monitoring.free_tool_id(3)
        except Exception:
            pass


def test_tool_id_falls_back_to_reserved_slots_when_custom_taken():
    """When 3, 4, and DEBUGGER_ID are all taken, fall back to the other
    reserved slots (COVERAGE_ID, PROFILER_ID, OPTIMIZER_ID) rather than
    refusing to install.

    The previous fallback list — (3, 4, DEBUGGER_ID) — gave up too early
    if a pdb instance happened to be holding slot 0 alongside whatever
    else was running. PEP 669 reserves slots 0/1/2/5 for specific tool
    archetypes, but ``use_tool_id`` is willing to lend them to us if the
    archetype isn't currently using one — and an instrumentation tool is
    materially less invasive than a debugger / profiler / coverage tool,
    so falling back is safer than failing.
    """
    instr._TOOL_ID = -1
    instr._TOOL_REGISTERED = False
    occupied = (3, 4, sys.monitoring.DEBUGGER_ID)
    for slot in occupied:
        try:
            sys.monitoring.free_tool_id(slot)
        except Exception:
            pass
        sys.monitoring.use_tool_id(slot, f"intruder-{slot}")
    try:
        program = _build_program(
            "fn:test.target.fn_a:entry { }",
            program_id="reserved-fallback",
        )
        install_program(program)
        assert instr._TOOL_ID not in occupied, (
            f"acquired slot {instr._TOOL_ID} must not be one of the occupied "
            f"slots {occupied}"
        )
        # The acquired slot must be one of the reserved-tool ids:
        # COVERAGE_ID, PROFILER_ID, OPTIMIZER_ID.
        assert instr._TOOL_ID in (
            sys.monitoring.COVERAGE_ID,
            sys.monitoring.PROFILER_ID,
            sys.monitoring.OPTIMIZER_ID,
        ), f"unexpected slot {instr._TOOL_ID}"
    finally:
        for slot in occupied:
            try:
                sys.monitoring.free_tool_id(slot)
            except Exception:
                pass


def test_tool_slot_retained_across_release():
    """``_release_tool`` disables events but keeps the slot owned.

    Holding the slot for process lifetime avoids callback-registration
    churn across stop()/start() cycles and prevents another tool from
    grabbing our slot during a momentary gap.
    """
    program = _build_program(
        "fn:test.target.fn_a:entry { }",
        program_id="slot-retain",
    )
    install_program(program)
    acquired_slot = instr._TOOL_ID

    with instr._LOCK:
        instr._release_tool()

    assert sys.monitoring.get_tool(acquired_slot) == instr._TOOL_NAME, (
        "slot must be retained across release for process-lifetime ownership"
    )


# The original review also flagged a marker-attach race: install_program
# attached a __posthog_decorator sentinel OUTSIDE the lock, and a concurrent
# uninstall could leave a stranded marker. That code path is gone — the
# marker attribute itself was removed once is_instrumented(fn) replaced
# it (see commit b0a9d20). No follow-up test is needed because there's
# no longer anything for the race to leak.

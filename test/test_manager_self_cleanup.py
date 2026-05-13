"""
Phase 3 — Registry & index consistency (hand-written tests, P2/P3).
Phase 4 — Self-cleanup convergence (P4).
Phase 5 — Order-independence (P5) hand-written + Hypothesis-driven tests.

The stateful machine that explores arbitrary install/uninstall/update
sequences lives in ``test_manager_registry_machine.py``.
"""

from __future__ import annotations

from typing import Any

import hypothesis.strategies as st
import pytest
from hogtrace.context import new_context
from hypothesis import given
from hypothesis import settings as hyp_settings

import libdebugger.instrumentation as instr
import libdebugger.manager as manager
from test._manager_helpers import (
    _build_program,
    _normalized_index,
    target_mod,
)
from test.strategies import programs as programs_strategy


# ---------------------------------------------------------------------------
# Shared fixture: many tests below need an active hogtrace request scope so
# the wrapper's probe path doesn't short-circuit on a missing store.
# ---------------------------------------------------------------------------


@pytest.fixture
def hogtrace_scope():
    """Provide a hogtrace request scope so _run_probes' get_store() is non-None."""
    with new_context():
        yield


# ---------------------------------------------------------------------------
# Phase 3 — Registry & index consistency (P2, P3)
# ---------------------------------------------------------------------------
#
# Hand-written tests for uninstall_program / update_program cover the common
# cases for clarity; the RuleBasedStateMachine in
# ``test_manager_registry_machine.py`` explores arbitrary install/uninstall/
# update sequences and asserts P2 + P3 invariants after every step.


def test_uninstall_program_removes_from_registry(hogtrace_scope):
    """After install + uninstall, both registries are empty."""
    from libdebugger.manager import install_program, uninstall_program

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog-u1",
    )
    install_program(program)
    assert "prog-u1" in instr._INSTALLED_PROGRAMS
    assert ("test.target.fn_a", "entry") in instr._PROBE_INDEX

    uninstall_program("prog-u1")

    assert instr._INSTALLED_PROGRAMS == {}
    assert instr._PROBE_INDEX == {}


def test_update_program_replaces_existing(hogtrace_scope):
    """update_program(B) where B.id == A.id replaces A's probes with B's."""
    from libdebugger.manager import install_program, update_program

    prog_a = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="same-id",
    )
    prog_b = _build_program(
        "fn:test.target.fn_b:entry { capture(x=2); }",
        program_id="same-id",
    )

    install_program(prog_a)
    assert instr._INSTALLED_PROGRAMS["same-id"] is prog_a
    assert ("test.target.fn_a", "entry") in instr._PROBE_INDEX
    assert ("test.target.fn_b", "entry") not in instr._PROBE_INDEX

    update_program(prog_b)

    assert instr._INSTALLED_PROGRAMS["same-id"] is prog_b
    # B's probes replaced A's: fn_a slot is gone, fn_b slot exists.
    assert ("test.target.fn_a", "entry") not in instr._PROBE_INDEX
    assert ("test.target.fn_b", "entry") in instr._PROBE_INDEX
    # And the program inside the index slot is the new B, not A.
    pairs = instr._PROBE_INDEX[("test.target.fn_b", "entry")]
    assert all(p is prog_b for p, _ in pairs)


def test_uninstall_unknown_program_id_is_silent():
    """Uninstalling a never-installed id must not raise."""
    from libdebugger.manager import uninstall_program

    # Precondition: registry is empty.
    assert instr._INSTALLED_PROGRAMS == {}

    # Must not raise.
    uninstall_program("never-installed")

    assert instr._INSTALLED_PROGRAMS == {}
    assert instr._PROBE_INDEX == {}


# ---------------------------------------------------------------------------
# Phase 4 — Self-cleanup convergence (P4)
# ---------------------------------------------------------------------------
#
# Property: after uninstalling every program targeting function F and then
# calling F once, hasattr(F, '__posthog_decorator') is False AND
# F.__code__ is original_code_for_F.
#
# Production-side support already landed in Phase 1 (self-uninstall block in
# InstrumentationDecorator.__call__'s finally, plus the name-mangling fix
# for delattr). These tests just cover the property end-to-end.


def test_self_cleanup_after_uninstall(hogtrace_scope):
    """Install -> uninstall -> call: wrapper self-cleans on next invocation.

    After uninstall_program but before the next call, the wrapper still
    sits on the function (cleanup is lazy). The call triggers the
    self-uninstall path in InstrumentationDecorator.__call__'s finally,
    after which both the marker attribute and the bytecode mutation are
    gone.
    """
    from libdebugger.manager import install_program, uninstall_program

    original_code = target_mod.fn_a.__code__

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog-p4-1",
    )
    install_program(program)

    # Wrapper exists, bytecode is mutated to the redirector.
    assert hasattr(target_mod.fn_a, "__posthog_decorator")
    assert target_mod.fn_a.__code__ is not original_code

    uninstall_program("prog-p4-1")

    # Cleanup is lazy: no call yet, so wrapper still on the function.
    assert hasattr(target_mod.fn_a, "__posthog_decorator"), (
        "self-cleanup must be lazy: wrapper survives uninstall until next call"
    )

    # First call after the registry emptied: self-uninstall fires.
    target_mod.fn_a(1)

    assert not hasattr(target_mod.fn_a, "__posthog_decorator"), (
        "P4: wrapper must self-clean on the next call after registry empties"
    )
    assert target_mod.fn_a.__code__ is original_code, (
        "P4: __code__ must be restored to original after self-cleanup"
    )


def test_self_cleanup_after_uninstall_via_update_with_different_target(hogtrace_scope):
    """Multi-program shared-function cleanup is keyed on per-qualname probe count.

    Program A and Program B both target fn_a (different program ids).
    Uninstall A: B still has a probe on fn_a, so the wrapper must NOT
    self-clean. Uninstall B: now NO probes target fn_a, so the next call
    self-cleans.
    """
    from libdebugger.manager import install_program, uninstall_program

    original_code = target_mod.fn_a.__code__

    prog_a = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog-a-share",
    )
    prog_b = _build_program(
        "fn:test.target.fn_a:exit { capture(x=2); }",
        program_id="prog-b-share",
    )
    install_program(prog_a)
    install_program(prog_b)

    assert hasattr(target_mod.fn_a, "__posthog_decorator")

    # Uninstall A only — B still has probes on fn_a.
    uninstall_program("prog-a-share")
    target_mod.fn_a(0)

    assert hasattr(target_mod.fn_a, "__posthog_decorator"), (
        "wrapper must persist while ANY program still targets the function"
    )
    assert target_mod.fn_a.__code__ is not original_code

    # Now uninstall B — registry slot for fn_a is empty.
    uninstall_program("prog-b-share")
    target_mod.fn_a(0)

    assert not hasattr(target_mod.fn_a, "__posthog_decorator"), (
        "wrapper must self-clean once no program targets the function"
    )
    assert target_mod.fn_a.__code__ is original_code


def test_self_cleanup_does_not_fire_during_update(hogtrace_scope):
    """update_program(B) with B.id == A.id: probes never go to zero across
    the swap, so the wrapper must not self-clean.

    The Phase 1 path defines update as uninstall + install, which means
    there's a brief window where the registry slot for the target may be
    empty. But the test never CALLS the function during that window, so
    the lazy self-cleanup never fires. The wrapper must still be in
    place after the update completes.
    """
    from libdebugger.manager import install_program, update_program

    original_code = target_mod.fn_a.__code__

    prog_a = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog-upd",
    )
    install_program(prog_a)

    # Replace A with B at the same id (same target qualname).
    prog_b = _build_program(
        "fn:test.target.fn_a:entry { capture(x=2); }",
        program_id="prog-upd",
    )
    update_program(prog_b)

    # Probes still exist for fn_a (now belonging to B). Wrapper persists.
    assert hasattr(target_mod.fn_a, "__posthog_decorator"), (
        "wrapper must persist across update — probes still exist on the target"
    )
    # And a call doesn't dislodge it, because the registry still has B's probes.
    target_mod.fn_a(0)
    assert hasattr(target_mod.fn_a, "__posthog_decorator"), (
        "wrapper must persist across update + call — B still has probes"
    )
    assert target_mod.fn_a.__code__ is not original_code

    # Note: update_program is uninstall + install, so decorator identity is
    # not asserted across the swap — only that the wrapper attribute survives.


def test_self_cleanup_preserves_other_wrappers(hogtrace_scope):
    """Cleanup is per-function. Uninstall affecting fn_b doesn't disturb fn_a.

    Install program A on fn_a AND program B on fn_b. Uninstall B. Call
    both functions: fn_a's wrapper stays (probes still there); fn_b's
    wrapper cleans up.
    """
    from libdebugger.manager import install_program, uninstall_program

    original_a = target_mod.fn_a.__code__
    original_b = target_mod.fn_b.__code__

    prog_a = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog-pres-a",
    )
    prog_b = _build_program(
        "fn:test.target.fn_b:entry { capture(x=2); }",
        program_id="prog-pres-b",
    )
    install_program(prog_a)
    install_program(prog_b)

    assert hasattr(target_mod.fn_a, "__posthog_decorator")
    assert hasattr(target_mod.fn_b, "__posthog_decorator")

    uninstall_program("prog-pres-b")

    # Both still wrapped at this point (no calls yet).
    assert hasattr(target_mod.fn_a, "__posthog_decorator")
    assert hasattr(target_mod.fn_b, "__posthog_decorator")

    # Call both. fn_a's probe persists -> stays wrapped. fn_b is orphaned
    # -> cleans up.
    target_mod.fn_a(0)
    target_mod.fn_b(0, 0)

    assert hasattr(target_mod.fn_a, "__posthog_decorator"), (
        "fn_a's wrapper must persist — its probe is still registered"
    )
    assert target_mod.fn_a.__code__ is not original_a
    assert not hasattr(target_mod.fn_b, "__posthog_decorator"), (
        "fn_b's wrapper must self-clean — its probe was uninstalled"
    )
    assert target_mod.fn_b.__code__ is original_b


# ---------------------------------------------------------------------------
# Phase 5 — Order-independence (P5)
# ---------------------------------------------------------------------------
#
# Property: for any permutation of a fixed multiset of install / uninstall /
# update operations that ends in the same final program set, the resulting
# ``_PROBE_INDEX`` is identical. In other words: ``_PROBE_INDEX`` is a pure
# function of ``_INSTALLED_PROGRAMS``.
#
# This is a structural consequence of ``_rebuild_probe_index`` iterating
# ``_INSTALLED_PROGRAMS`` afresh on every reconcile — so no production-code
# changes are expected. The tests below pin the property so a future
# refactor that introduces order-sensitivity gets caught immediately.


def test_probe_index_is_pure_function_of_installed_programs(hogtrace_scope):
    """Four operation sequences converging on the same final program set
    must produce the same ``_PROBE_INDEX``.

    Sequences exercised:
      A: install P1, install P2, install P3.
      B: install P3, install P1, install P2.        (different order)
      C: install P1, install P2, install P3,
         uninstall P1, install P1.                  (transient removal)
      D: install P1, install P2, install P3,
         install P2 (overwrite at same id).         (overwrite path)

    All four end with ``{P1.id, P2.id, P3.id}`` installed, so the normalized
    ``_PROBE_INDEX`` views must coincide.
    """

    def _drain() -> None:
        for pid in list(instr._INSTALLED_PROGRAMS):
            manager.uninstall_program(pid)

    # Use distinct specifiers so each program contributes a distinct slot —
    # makes test failures easier to diagnose (you can tell which slot
    # diverged).
    p1 = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="p5-p1",
    )
    p2 = _build_program(
        "fn:test.target.fn_b:entry { capture(x=2); }",
        program_id="p5-p2",
    )
    p3 = _build_program(
        "fn:test.target.fn_c:exit { capture(x=3); }",
        program_id="p5-p3",
    )

    # Sequence A: canonical install order.
    _drain()
    manager.install_program(p1)
    manager.install_program(p2)
    manager.install_program(p3)
    index_a = _normalized_index()

    # Sequence B: reversed install order.
    _drain()
    manager.install_program(p3)
    manager.install_program(p1)
    manager.install_program(p2)
    index_b = _normalized_index()

    # Sequence C: install all, transiently uninstall p1, reinstall p1.
    _drain()
    manager.install_program(p1)
    manager.install_program(p2)
    manager.install_program(p3)
    manager.uninstall_program("p5-p1")
    manager.install_program(p1)
    index_c = _normalized_index()

    # Sequence D: install p1, p2, p3 then overwrite p2 with itself (same id,
    # same payload). install_program is documented to overwrite a same-id
    # install, so the registry ends up with the SAME three programs.
    _drain()
    manager.install_program(p1)
    manager.install_program(p2)
    manager.install_program(p3)
    manager.install_program(p2)
    index_d = _normalized_index()

    # All four sequences must converge to the same normalized index.
    assert index_a == index_b, f"A vs B diverged. A={index_a!r} B={index_b!r}"
    assert index_a == index_c, f"A vs C diverged. A={index_a!r} C={index_c!r}"
    assert index_a == index_d, f"A vs D diverged. A={index_a!r} D={index_d!r}"

    # Sanity: the converged index is non-trivial (each program contributed
    # one slot). Guards against a vacuous-equality bug where every sequence
    # somehow ended up with an empty registry.
    assert set(index_a.keys()) == {
        ("test.target.fn_a", "entry"),
        ("test.target.fn_b", "entry"),
        ("test.target.fn_c", "exit"),
    }


@given(data=st.data())
@hyp_settings(max_examples=30, deadline=None)
def test_probe_index_pure_function_of_program_set(data):
    """Hypothesis-driven: for any program set, two different install orders
    yield the same ``_PROBE_INDEX``.

    Strategy:
      1. Draw a list of programs with distinct ids.
      2. Draw a Hypothesis-controlled permutation of that list — so any
         failure is deterministically replayable.
      3. Install in original order; snapshot normalized index as ``A``.
      4. Drain. Install in shuffled order; snapshot normalized index as ``B``.
      5. Assert ``A == B``.

    The drain step is essential — Hypothesis runs many examples in one
    pytest case and we need a clean slate between sequences.

    We enter the hogtrace request scope inside the body (not via a
    function-scoped fixture) because Hypothesis warns about fixture reuse
    across generated inputs. ``install_program`` doesn't need an active
    scope per se — it only registers probes — but we use one anyway so
    the wrapper installation path is exercised in a realistic environment.
    """
    program_set: list[Any] = data.draw(
        st.lists(
            programs_strategy(probes_max=3),
            min_size=1,
            max_size=5,
            unique_by=lambda p: p.id,
        ),
        label="program_set",
    )
    shuffled = data.draw(st.permutations(program_set), label="shuffled")

    def _drain() -> None:
        for pid in list(instr._INSTALLED_PROGRAMS):
            manager.uninstall_program(pid)

    with new_context():
        # Sequence A: install in the order the strategy produced.
        _drain()
        for p in program_set:
            manager.install_program(p)
        index_a = _normalized_index()

        # Sequence B: install in the shuffled order.
        _drain()
        for p in shuffled:
            manager.install_program(p)
        index_b = _normalized_index()

        assert index_a == index_b, (
            f"_PROBE_INDEX depends on install order. "
            f"original={[p.id for p in program_set]} "
            f"shuffled={[p.id for p in shuffled]} "
            f"index_a={index_a!r} index_b={index_b!r}"
        )

        # Final-state hygiene: drain so subsequent Hypothesis examples (and
        # the autouse ``reset_state`` fixture) start clean.
        _drain()

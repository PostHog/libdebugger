"""
Phase 6 — Recursion safety (P6).

Property: for a function F that calls itself N times within one outer call,
``_enqueue_message`` fires exactly ``N * (entry_count + exit_count)`` times.
No deadlock, no missed probes, no double-firing.

The frame-stack handling in ``InstrumentationDecorator.__call__`` is what
makes recursion work — each call pushes its own frame, and the
``previous_frame_top`` check distinguishes "we pushed a new frame this call"
from "we didn't." These tests pin that behavior under increasing depth and
under an exception mid-recursion.

Invocation count for ``test.target.fact``:
  fact(N) when N <= 1 returns 1 directly (base case) -> 1 invocation.
  fact(N) when N >= 2 recurses to fact(N-1) -> N invocations total.
  So total invocations = max(N, 1).
"""

from __future__ import annotations

import hypothesis.strategies as st
import pytest
from hogtrace.context import new_context
from hypothesis import given, settings

import libdebugger.instrumentation as instr
import libdebugger.manager as manager
from test._manager_helpers import (
    _build_program,
    _expected_fact_invocations,
    target_mod,
)


@pytest.fixture
def hogtrace_scope():
    """Provide a hogtrace request scope so _run_probes' get_store() is non-None."""
    with new_context():
        yield


@pytest.fixture
def capture_enqueue(monkeypatch):
    """Replace _enqueue_message with a list-recording stub."""
    calls = []

    def _stub(program, probe, captures):
        calls.append((program, probe, captures))

    monkeypatch.setattr(instr, "_enqueue_message", _stub)
    return calls


def test_recursion_entry_probe_fires_N_times(hogtrace_scope, capture_enqueue):
    """One entry probe on fact -> exactly ``max(N, 1)`` enqueues per outer call.

    Spans the interesting boundary cases (N=0, N=1 base case; N=2 first true
    recursion; N=5 and N=10 deeper recursion) so a frame-stack bug that only
    manifests past depth 1 still gets caught.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fact:entry { capture(hit=1); }",
        program_id="prog-rec-entry",
    )
    install_program(program)
    assert hasattr(target_mod.fact, "__posthog_decorator")

    for n in (0, 1, 2, 5, 10):
        del capture_enqueue[:]  # reset between sub-cases
        target_mod.fact(n)

        expected = _expected_fact_invocations(n)
        assert len(capture_enqueue) == expected, (
            f"fact({n}): expected {expected} entry-probe fires, "
            f"got {len(capture_enqueue)}"
        )
        # Every fire must be the entry probe (not exit, not something else).
        for _prog, probe, _captures in capture_enqueue:
            assert probe.spec.target == "entry", (
                f"fact({n}): expected only entry fires; got target={probe.spec.target}"
            )


def test_recursion_entry_and_exit_probes_fire_2N_times(hogtrace_scope, capture_enqueue):
    """One entry + one exit probe on fact -> exactly ``2 * max(N, 1)`` fires.

    Verifies the total count AND that each invocation got both an entry and
    an exit fire — counting the per-target totals separately catches a bug
    where (e.g.) the exit probe fires twice and the entry never fires but
    the total looks right.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fact:entry { capture(hit=1); }\n"
        "fn:test.target.fact:exit { capture(hit=2); }",
        program_id="prog-rec-entry-exit",
    )
    install_program(program)

    for n in (0, 1, 2, 5, 10):
        del capture_enqueue[:]
        target_mod.fact(n)

        invocations = _expected_fact_invocations(n)
        expected_total = 2 * invocations

        assert len(capture_enqueue) == expected_total, (
            f"fact({n}): expected {expected_total} fires "
            f"(entry+exit per invocation); got {len(capture_enqueue)}"
        )

        entry_fires = [c for c in capture_enqueue if c[1].spec.target == "entry"]
        exit_fires = [c for c in capture_enqueue if c[1].spec.target == "exit"]

        assert len(entry_fires) == invocations, (
            f"fact({n}): entry must fire once per invocation "
            f"(expected {invocations}); got {len(entry_fires)}"
        )
        assert len(exit_fires) == invocations, (
            f"fact({n}): exit must fire once per invocation "
            f"(expected {invocations}); got {len(exit_fires)}"
        )


def test_recursion_under_exception_still_fires_exit_probes(
    hogtrace_scope, capture_enqueue
):
    """recur_raise(N) raises at the base case but every level that entered
    must also exit (with ``exception=`` set).

    For ``recur_raise(N)`` with N >= 1, the call chain is:
      recur_raise(N), recur_raise(N-1), ..., recur_raise(0)
    so total invocations is N+1. Every invocation enters (entry fires) and
    every invocation unwinds under the same ValueError (exit fires in the
    wrapper's ``finally``).

    The base case raises BEFORE recursing, so its entry probe fires before
    the exception propagates — exit must still fire for it as well.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.recur_raise:entry { capture(hit=1); }\n"
        "fn:test.target.recur_raise:exit { capture(hit=2); }",
        program_id="prog-rec-raise",
    )
    install_program(program)

    for n in (0, 1, 3, 5):
        del capture_enqueue[:]
        with pytest.raises(ValueError, match="recur_raise base case"):
            target_mod.recur_raise(n)

        # n=0 is one invocation (the base case itself).
        # n>=1: invocations = n + 1 (n recursive calls + 1 base case).
        invocations = n + 1
        expected_total = 2 * invocations

        assert len(capture_enqueue) == expected_total, (
            f"recur_raise({n}): expected {expected_total} fires "
            f"(entry+exit per level); got {len(capture_enqueue)}"
        )

        entry_fires = [c for c in capture_enqueue if c[1].spec.target == "entry"]
        exit_fires = [c for c in capture_enqueue if c[1].spec.target == "exit"]

        assert len(entry_fires) == invocations, (
            f"recur_raise({n}): every entered level must have its entry probe "
            f"fire (expected {invocations}); got {len(entry_fires)}"
        )
        assert len(exit_fires) == invocations, (
            f"recur_raise({n}): every entered level must also exit "
            f"(expected {invocations}); got {len(exit_fires)}"
        )


def test_recursion_does_not_deadlock(hogtrace_scope, capture_enqueue):
    """fact(50) with a probe installed completes in well under 5 seconds.

    Tripwire for accidental locking issues on the hot path. The wrapper's
    ``__call__`` acquires ``self._lock`` only on the rebuild path (and the
    self-uninstall path when the registry empties). Under recursion with a
    stable registry neither path should be taken, so no lock contention
    should occur. If this test ever times out, suspect a lock acquired
    unconditionally on every call.
    """
    import time

    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fact:entry { capture(hit=1); }",
        program_id="prog-rec-deadlock",
    )
    install_program(program)

    start = time.monotonic()
    target_mod.fact(50)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, (
        f"fact(50) took {elapsed:.3f}s — suspect deadlock or lock contention"
    )
    # And the call count is right (50 invocations).
    assert len(capture_enqueue) == _expected_fact_invocations(50)


@given(depth=st.integers(min_value=0, max_value=50))
@settings(max_examples=20, deadline=None)
def test_recursion_probe_count_proportional_to_depth(depth):
    """For any depth N in [0, 50], fact(N) with one entry + one exit probe
    fires ``_enqueue_message`` exactly ``2 * max(N, 1)`` times.

    Hypothesis-driven sanity check on the per-call linearity of probe firing.
    Each example installs a fresh program, exercises recursion, then drains
    the registry — Hypothesis runs many examples in one pytest case and the
    autouse ``reset_state`` fixture only fires between cases.
    """
    # Build a fresh capture list and patch _enqueue_message for this example.
    # We don't use the ``capture_enqueue`` fixture because it's function-scoped
    # and Hypothesis examples share the function scope.
    calls = []
    original_enqueue = instr._enqueue_message

    def _stub(program, probe, captures):
        calls.append((program, probe, captures))

    instr._enqueue_message = _stub
    try:
        with new_context():
            program = _build_program(
                "fn:test.target.fact:entry { capture(hit=1); }\n"
                "fn:test.target.fact:exit { capture(hit=2); }",
                program_id="prog-rec-hyp",
            )
            try:
                manager.install_program(program)
                target_mod.fact(depth)
            finally:
                manager.uninstall_program("prog-rec-hyp")
                # Force lazy self-cleanup so the next Hypothesis example
                # starts from an unwrapped function.
                fn = manager.resolve_target("test.target.fact")
                if fn is not None and hasattr(fn, "__posthog_decorator"):
                    try:
                        target_mod.fact(0)
                    except Exception:
                        pass
    finally:
        instr._enqueue_message = original_enqueue

    invocations = _expected_fact_invocations(depth)
    expected = 2 * invocations
    assert len(calls) == expected, (
        f"fact({depth}): expected {expected} fires; got {len(calls)}"
    )

    entry_fires = [c for c in calls if c[1].spec.target == "entry"]
    exit_fires = [c for c in calls if c[1].spec.target == "exit"]
    assert len(entry_fires) == invocations
    assert len(exit_fires) == invocations

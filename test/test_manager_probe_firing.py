"""
Phase 2 — Trace fidelity (P1).

Property: for a single call,
  _enqueue_message is invoked exactly
      len(entry_probes_for_fn) + len(exit_probes_fired_for_fn)
  times.
- Normal return: both entry and exit probes fire.
- Exception: entry fires; exit fires with ``exception=`` set.
- Entry never fires twice for one call.

These tests bypass the strategies.programs() randomness so we can pin down
exactly which probes a program carries. We compile one-probe programs
directly via hogtrace.vm.compile / package and feed them to install_program.
"""

from __future__ import annotations

import pytest
from hogtrace.context import new_context

import libdebugger.instrumentation as instr
from test._manager_helpers import _build_program, target_mod


@pytest.fixture
def hogtrace_scope():
    """Provide a hogtrace request scope so _run_probes' get_store() is non-None."""
    with new_context():
        yield


@pytest.fixture
def capture_enqueue(monkeypatch):
    """Replace _enqueue_message with a list-recording stub.

    Returns the list of (program, probe, captures) triples observed.
    """
    calls = []

    def _stub(program, probe, captures):
        calls.append((program, probe, captures))

    monkeypatch.setattr(instr, "_enqueue_message", _stub)
    return calls


def test_entry_probe_fires_once_per_call(hogtrace_scope, capture_enqueue):
    """One entry probe on fn_a -> one _enqueue_message call per invocation."""
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(hit=1); }",
        program_id="prog-entry",
    )
    install_program(program)

    # Sanity: registry populated, fn instrumented.
    assert instr._INSTALLED_PROGRAMS["prog-entry"] is program
    assert ("test.target.fn_a", "entry") in instr._PROBE_INDEX
    assert instr.is_instrumented(target_mod.fn_a)

    # Single call -> exactly one entry-probe enqueue.
    target_mod.fn_a(7)

    assert len(capture_enqueue) == 1, (
        f"expected exactly 1 enqueue, got {len(capture_enqueue)}"
    )
    prog, probe, _captures = capture_enqueue[0]
    assert prog is program
    assert probe.spec.specifier == "test.target.fn_a"
    assert probe.spec.target == "entry"


def test_exit_probe_fires_on_normal_return(hogtrace_scope, capture_enqueue):
    """One exit probe on fn_a -> one _enqueue_message on normal return."""
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_a:exit { capture(hit=1); }",
        program_id="prog-exit",
    )
    install_program(program)

    result = target_mod.fn_a(5)
    assert result == 1 + 2 + 5  # behavior preservation cross-check

    assert len(capture_enqueue) == 1
    prog, probe, _captures = capture_enqueue[0]
    assert prog is program
    assert probe.spec.target == "exit"


def test_entry_and_exit_both_fire_on_normal_return(hogtrace_scope, capture_enqueue):
    """Entry + exit probes on same fn -> exactly 2 enqueues, in entry-then-exit order."""
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(hit=1); }\n"
        "fn:test.target.fn_a:exit { capture(hit=2); }",
        program_id="prog-both",
    )
    install_program(program)

    target_mod.fn_a(3)

    assert len(capture_enqueue) == 2, (
        f"expected exactly 2 enqueues, got {len(capture_enqueue)}"
    )
    targets = [c[1].spec.target for c in capture_enqueue]
    assert targets == ["entry", "exit"], f"entry must precede exit; got {targets}"


def test_exit_probe_fires_on_exception(hogtrace_scope, capture_enqueue):
    """Exit probe on a function that raises still fires (with exception passed in)."""
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_raises:exit { capture(ok=1); }",
        program_id="prog-raise",
    )
    install_program(program)

    with pytest.raises(ValueError, match="boom"):
        target_mod.fn_raises()

    # The failure mode this test targets is "wrapper never takes the
    # exit-probe path on exception". A vacuous pass (no calls at all) would
    # hide that bug, so assert the call count BEFORE iterating and confirm
    # the single call is in fact the exit probe.
    assert len(capture_enqueue) == 1, (
        f"exit probe must fire once on exception, got {len(capture_enqueue)} fires"
    )
    prog, probe, _captures = capture_enqueue[0]
    assert prog is program
    assert probe.spec.target == "exit", (
        f"the firing probe must be the exit probe, got target={probe.spec.target}"
    )


def test_entry_fires_once_on_exception(hogtrace_scope, capture_enqueue):
    """Even when the function raises, the entry probe fires exactly once."""
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_raises:entry { capture(ok=1); }",
        program_id="prog-raise-entry",
    )
    install_program(program)

    with pytest.raises(ValueError, match="boom"):
        target_mod.fn_raises()

    entry_calls = [c for c in capture_enqueue if c[1].spec.target == "entry"]
    assert len(entry_calls) == 1, (
        f"entry must fire exactly once on raise; got {len(entry_calls)}"
    )


def test_multiple_programs_on_same_function(hogtrace_scope, capture_enqueue):
    """Two programs each with entry probe on fn_a -> 2 enqueues per call."""
    from libdebugger.manager import install_program

    prog1 = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog-a",
    )
    prog2 = _build_program(
        "fn:test.target.fn_a:entry { capture(x=2); }",
        program_id="prog-b",
    )
    install_program(prog1)
    install_program(prog2)

    # Only one wrapper, but two probes registered for the same key.
    entry_probes = instr._PROBE_INDEX[("test.target.fn_a", "entry")]
    assert len(entry_probes) == 2

    target_mod.fn_a(0)

    assert len(capture_enqueue) == 2
    program_ids = {c[0].id for c in capture_enqueue}
    assert program_ids == {"prog-a", "prog-b"}


def test_install_program_keeps_function_instrumented_across_installs(
    hogtrace_scope, capture_enqueue
):
    """Two programs targeting the same fn keep ``is_instrumented`` True throughout.

    Each install merges into the dispatch index without disturbing the
    monitoring mask on the target's code object.
    """
    from libdebugger.manager import install_program

    prog1 = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog-shared-1",
    )
    prog2 = _build_program(
        "fn:test.target.fn_a:exit { capture(hit=2); }",
        program_id="prog-shared-2",
    )
    install_program(prog1)
    assert instr.is_instrumented(target_mod.fn_a)
    monitored_after_first = instr._MONITORED_CODES.get(target_mod.fn_a.__code__)

    install_program(prog2)
    assert instr.is_instrumented(target_mod.fn_a)
    # Mask grew (exit added) but the code stays monitored without churn.
    assert instr._MONITORED_CODES.get(target_mod.fn_a.__code__) is not None
    assert (
        instr._MONITORED_CODES[target_mod.fn_a.__code__] != monitored_after_first
        or monitored_after_first is None
    )


def test_unresolvable_specifier_logs_and_continues(
    hogtrace_scope, capture_enqueue, caplog
):
    """A probe whose target can't be resolved must not crash install_program."""
    import logging
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:libdebugger.no_such_module.no_fn:entry { capture(x=1); }",
        program_id="prog-bogus",
    )

    caplog.set_level(logging.WARNING, logger="libdebugger.manager")
    install_program(program)  # must not raise

    # Registry still records the program even though the wrapper was never installed.
    assert "prog-bogus" in instr._INSTALLED_PROGRAMS
    # The (qualname, "entry") slot exists; just no fn was wrapped.
    assert ("libdebugger.no_such_module.no_fn", "entry") in instr._PROBE_INDEX

    # A warning was logged.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("not resolvable" in r.getMessage() for r in warnings), (
        f"expected 'not resolvable' warning; got {[r.getMessage() for r in warnings]}"
    )


def test_resolve_target_module_function():
    """resolve_target finds top-level module functions."""
    from libdebugger.manager import resolve_target

    fn = resolve_target("test.target.fn_a")
    assert fn is target_mod.fn_a


def test_resolve_target_class_method():
    """resolve_target finds class methods via dotted-name walk."""
    from libdebugger.manager import resolve_target

    fn = resolve_target("test.target.Klass.method")
    assert fn is target_mod.Klass.method


def test_resolve_target_returns_none_for_missing():
    """resolve_target returns None (no raise) for non-resolvable specifiers."""
    from libdebugger.manager import resolve_target

    assert resolve_target("test.target.does_not_exist") is None
    assert resolve_target("nonexistent.module.fn") is None


def test_resolve_target_returns_none_for_module():
    """resolve_target returns None when the specifier names a module (not a callable)."""
    from libdebugger.manager import resolve_target

    # ``test.target`` is itself a module, not a callable.
    assert resolve_target("test.target") is None


# The tuple-reuse drift-detection test that used to live here is gone —
# the bytecode-rewriting wrapper that identity-compared probe tuples no
# longer exists. The rebuild can (and does) build a fresh tuple every
# time. ``test_manager_self_cleanup.py`` covers the invariant that
# matters now: ``_PROBE_INDEX`` is a pure function of
# ``_INSTALLED_PROGRAMS``.

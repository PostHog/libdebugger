"""
Property tests for the hogtrace-manager rewrite.

Phase 1 — Behavior preservation (P7): An instrumented function returns
the same value (or raises the same exception) as its uninstrumented
counterpart, modulo probe side effects. With no probes installed at all,
wrapping and unwrapping a function must be a perfect no-op observable to
callers.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, FrozenSet, Iterable, Tuple

import hypothesis.strategies as st
import pytest
from hogtrace.context import new_context
from hogtrace.vm import compile as ht_compile, package as ht_package
from hypothesis import given, settings
from hypothesis import settings as hyp_settings
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    rule,
)

import libdebugger.instrumentation as instr
import libdebugger.manager as manager
from libdebugger.instrumentation import InstrumentationDecorator
from test.strategies import _SPECIFIER_POOL, programs as programs_strategy


def test_imports_clean():
    """Sanity check that the production modules import without error."""
    import libdebugger.instrumentation  # noqa: F401
    import libdebugger.manager  # noqa: F401
    from test import strategies, target  # noqa: F401


# ---------------------------------------------------------------------------
# Phase 1 — Behavior preservation (P7)
# ---------------------------------------------------------------------------
#
# We pair each target function with (a) a strategy that produces valid
# args for it and (b) the qualname string that identifies it. The
# qualname is unused in Phase 1 (the registry is always empty), but the
# decorator's new constructor takes it, so we pass the canonical value.

target_mod = importlib.import_module("test.target")


def _fn_a_args() -> st.SearchStrategy[Tuple[Any, ...]]:
    return st.tuples(st.integers(min_value=-1000, max_value=1000))


def _fn_b_args() -> st.SearchStrategy[Tuple[Any, ...]]:
    return st.tuples(
        st.integers(min_value=-1000, max_value=1000),
        st.integers(min_value=-1000, max_value=1000),
    )


def _fn_c_args() -> st.SearchStrategy[Tuple[Any, ...]]:
    return st.tuples(st.text(max_size=20))


def _fn_d_args() -> st.SearchStrategy[Tuple[Any, ...]]:
    return st.tuples(
        st.one_of(
            st.none(),
            st.lists(st.integers(), max_size=10),
        ),
    )


def _fn_e_args() -> st.SearchStrategy[Tuple[Any, ...]]:
    return st.just(())


def _fact_args() -> st.SearchStrategy[Tuple[Any, ...]]:
    # Hard-cap depth to keep recursion sane; fact() also caps internally.
    return st.tuples(st.integers(min_value=0, max_value=20))


# Each entry: (function-getter, qualname, args-strategy).
#
# We use getters rather than function refs directly so that the reset_state
# fixture's cleanup runs against the same module attribute we're wrapping.
TARGETS = [
    (lambda: target_mod.fn_a, "test.target.fn_a", _fn_a_args()),
    (lambda: target_mod.fn_b, "test.target.fn_b", _fn_b_args()),
    (lambda: target_mod.fn_c, "test.target.fn_c", _fn_c_args()),
    (lambda: target_mod.fn_d, "test.target.fn_d", _fn_d_args()),
    (lambda: target_mod.fn_e, "test.target.fn_e", _fn_e_args()),
    (lambda: target_mod.fact, "test.target.fact", _fact_args()),
]


def _unwrap(fn: Callable[..., Any]) -> None:
    """Tear down whatever ``__posthog_decorator`` the test set up."""
    dec = getattr(fn, "__posthog_decorator", None)
    if dec is not None:
        try:
            dec.cleanup()
        finally:
            try:
                delattr(fn, "__posthog_decorator")
            except AttributeError:
                pass


@pytest.mark.parametrize(
    "fn_getter,qualname,args_strategy",
    TARGETS,
    ids=[q for _, q, _ in TARGETS],
)
def test_wrap_unwrap_preserves_behavior(fn_getter, qualname, args_strategy):
    """For each pool function, wrapping with no probes is a no-op.

    Compute expected from the uninstrumented function, wrap with the
    decorator (qualname-only constructor — registry is empty), call the
    wrapped function TWICE and assert equality, then unwrap and call again.

    The double call matters: a degenerate wrapper that restores the
    original on the first invocation and forwards thereafter would pass
    a single-call equality check. Calling twice catches that class of
    failure (and is also what triggers the self-uninstall path in the
    real wrapper, which we want exercised here).
    """
    fn = fn_getter()

    @given(args=args_strategy)
    @settings(max_examples=25, deadline=None)
    def _inner(args):
        # 1. Compute expected BEFORE wrapping.
        expected = fn(*args)

        # 2. Wrap.
        assert not hasattr(fn, "__posthog_decorator"), (
            "test setup invariant: function must not be pre-wrapped"
        )
        try:
            fn.__posthog_decorator = InstrumentationDecorator(fn, qualname=qualname)

            # 3. Wrapped call equals expected. Call twice — catches
            # "wrapper degrades after first call" failures and exercises
            # the self-uninstall path on the second invocation.
            assert fn(*args) == expected
            assert fn(*args) == expected
        finally:
            # 4. Unwrap and confirm post-unwrap behavior also matches.
            _unwrap(fn)

        # 5. After unwrap, the function still returns the same value.
        assert fn(*args) == expected

    _inner()


def test_wrap_unwrap_preserves_exception():
    """If the wrapped function raises, the instrumented version raises the same.

    No probe-side effects to consider in Phase 1; the registry is empty,
    so the wrapper's only job is to faithfully forward the exception.

    The wrapped function is raised+caught TWICE while wrapped so the
    second raise proves the wrapper still raises correctly even if the
    self-uninstall path fired during the first call.
    """

    # Add a function that raises. We define it locally so the
    # reset_state fixture can't fail to clean it up (it's not on the
    # target module).
    def fn_raises():
        raise ValueError("boom")

    # Confirm the un-wrapped behavior first.
    with pytest.raises(ValueError, match="boom"):
        fn_raises()

    fn_raises.__posthog_decorator = InstrumentationDecorator(
        fn_raises, qualname="test.local.fn_raises"
    )
    try:
        with pytest.raises(ValueError, match="boom"):
            fn_raises()
        with pytest.raises(ValueError, match="boom"):
            fn_raises()
    finally:
        _unwrap(fn_raises)

    # After unwrap, still raises.
    with pytest.raises(ValueError, match="boom"):
        fn_raises()


def test_wrap_unwrap_method_on_class():
    """Bound methods unwrap to the underlying function; wrapping works."""
    klass_instance = target_mod.Klass()
    expected_3 = klass_instance.method(3)
    expected_minus_2 = klass_instance.method(-2)

    # Wrap the bound method; the decorator unwraps to the underlying
    # function and the attribute lands on Klass.method.
    klass_method = target_mod.Klass.method
    klass_method.__posthog_decorator = InstrumentationDecorator(
        klass_instance.method, qualname="test.target.Klass.method"
    )
    try:
        assert klass_instance.method(3) == expected_3
        assert klass_instance.method(-2) == expected_minus_2
    finally:
        _unwrap(klass_method)

    # And post-unwrap.
    assert klass_instance.method(3) == expected_3


def test_self_uninstall_removes_marker_attribute():
    """Regression test: self-uninstall must remove ``__posthog_decorator``.

    Prior to the fix, ``InstrumentationDecorator.__call__`` used
    ``del self.wrapped_fn.__posthog_decorator`` inside the class body.
    Python name-mangling rewrites that to
    ``_InstrumentationDecorator__posthog_decorator`` which never matches
    the attribute the caller set, so the ``except AttributeError`` path
    silently swallowed the failure and the marker attribute survived.

    With ``_PROBE_INDEX`` empty (Phase 1 default), the first call to a
    wrapped function takes the self-uninstall branch; afterward the
    function must no longer carry the marker.
    """
    fn = target_mod.fn_a

    # Sanity preconditions.
    assert not hasattr(fn, "__posthog_decorator"), (
        "test invariant: fn must not be pre-wrapped"
    )
    assert instr._PROBE_INDEX == {}, (
        "test invariant: registry must be empty so self-uninstall fires"
    )

    fn.__posthog_decorator = InstrumentationDecorator(fn, qualname="test.target.fn_a")
    try:
        assert hasattr(fn, "__posthog_decorator")

        # First (and only) call: registry is empty, so __call__'s finally
        # block should take the self-uninstall branch and delete the
        # marker attribute via ``delattr(..., "__posthog_decorator")``.
        fn(1)

        assert not hasattr(fn, "__posthog_decorator"), (
            "self-uninstall should have removed the marker attribute "
            "(name-mangling regression)"
        )
    finally:
        _unwrap(fn)


def test_module_globals_present():
    """Phase 1 production-code invariant: registry globals exist as empty dicts."""
    assert hasattr(instr, "_PROBE_INDEX")
    assert hasattr(instr, "_INSTALLED_PROGRAMS")
    assert hasattr(instr, "_LOCK")
    # Both registries start empty.
    assert instr._PROBE_INDEX == {}
    assert instr._INSTALLED_PROGRAMS == {}


# ---------------------------------------------------------------------------
# Phase 2 — Trace fidelity (P1)
# ---------------------------------------------------------------------------
#
# Property: for a single call,
#   _enqueue_message is invoked exactly
#       len(entry_probes_for_fn) + len(exit_probes_fired_for_fn)
#   times.
# - Normal return: both entry and exit probes fire.
# - Exception: entry fires; exit fires with ``exception=`` set.
# - Entry never fires twice for one call.
#
# These tests bypass the strategies.programs() randomness so we can pin down
# exactly which probes a program carries. We compile one-probe programs
# directly via hogtrace.vm.compile / package and feed them to install_program.


def _build_program(source: str, program_id: str = "test-prog"):
    """Compile a single hogtrace source snippet into a packaged Program."""
    return ht_package(program_id, ht_compile(source))


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

    # Sanity: registry populated, fn wrapped.
    assert instr._INSTALLED_PROGRAMS["prog-entry"] is program
    assert ("test.target.fn_a", "entry") in instr._PROBE_INDEX
    assert hasattr(target_mod.fn_a, "__posthog_decorator")

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


def test_install_program_creates_wrapper_only_once(hogtrace_scope, capture_enqueue):
    """Installing two programs targeting the same fn shares one wrapper instance."""
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
    dec_after_first = target_mod.fn_a.__posthog_decorator
    install_program(prog2)
    dec_after_second = target_mod.fn_a.__posthog_decorator

    assert dec_after_first is dec_after_second, (
        "wrapper should be created on first install and reused on subsequent installs"
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


def test_rebuild_probe_index_reuses_tuple_when_unchanged():
    """When _rebuild_probe_index produces the same content, it reuses the prior tuple object.

    This is load-bearing for Phase 6: the wrapper's hot path identity-compares
    line-probe tuples to detect drift. If _rebuild_probe_index always builds
    a new tuple even when content is unchanged, identity-compare fires on
    every reconcile and we rebuild instrumented_fn unnecessarily.
    """
    from libdebugger.manager import install_program, _rebuild_probe_index

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog-stable",
    )
    install_program(program)

    snapshot_a = instr._PROBE_INDEX[("test.target.fn_a", "entry")]

    # Rebuild from the same _INSTALLED_PROGRAMS state — contents unchanged.
    with instr._LOCK:
        _rebuild_probe_index()

    snapshot_b = instr._PROBE_INDEX[("test.target.fn_a", "entry")]

    assert snapshot_a is snapshot_b, (
        "tuple objects must be reused when contents are unchanged"
    )


# ---------------------------------------------------------------------------
# Phase 3 — Registry & index consistency (P2, P3)
# ---------------------------------------------------------------------------
#
# Hand-written tests for uninstall_program / update_program cover the common
# cases for clarity; the RuleBasedStateMachine below explores arbitrary
# install/uninstall/update sequences and asserts P2 + P3 invariants after
# every step.


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
# Stateful machine — Hypothesis explores arbitrary install/uninstall/update
# sequences and checks the P2/P3 invariants after every step.
# ---------------------------------------------------------------------------


def _drain_registry():
    """Tear down everything in the registry plus any lingering wrappers.

    Used as cross-round cleanup inside the stateful machine — Hypothesis
    runs many examples within a single pytest invocation and the
    ``reset_state`` fixture only fires between pytest test cases, not
    between Hypothesis examples.
    """
    for pid in list(instr._INSTALLED_PROGRAMS):
        # No broad except: uninstall_program is the function under test;
        # swallowing exceptions here would hide real regressions.
        manager.uninstall_program(pid)

    # Also tear down any wrapper still attached to a target function. The
    # production code leaves these in place until next-call self-cleanup;
    # we need them gone between rounds so a stale wrapper from round N
    # doesn't pollute round N+1's invariant check.
    for _name, obj in list(vars(target_mod).items()):
        if hasattr(obj, "__posthog_decorator"):
            dec = getattr(obj, "__posthog_decorator")
            try:
                dec.cleanup()
            except Exception:
                pass
            try:
                delattr(obj, "__posthog_decorator")
            except AttributeError:
                pass
        if isinstance(obj, type):
            for _mname, mobj in list(vars(obj).items()):
                if hasattr(mobj, "__posthog_decorator"):
                    dec = getattr(mobj, "__posthog_decorator")
                    try:
                        dec.cleanup()
                    except Exception:
                        pass
                    try:
                        delattr(mobj, "__posthog_decorator")
                    except AttributeError:
                        pass


# Deterministic arg providers for each specifier in _SPECIFIER_POOL. Used by
# the stateful machine's call_function rule — Hypothesis strategies inside a
# @rule body would re-roll on each call (or fail to draw at all) so we use
# plain Python literals instead. The values are arbitrary but valid for each
# target's signature.
_CALL_ARGS_BY_SPECIFIER: Dict[str, Tuple[Any, ...]] = {
    "test.target.fn_a": (1,),
    "test.target.fn_b": (1, 2),
    "test.target.fn_c": ("x",),
    "test.target.fn_d": ([1, 2, 3],),
    "test.target.fn_e": (),
    "test.target.Klass.method": (3,),
    # fact(0) returns 1 with no recursion; small + safe.
    "test.target.fact": (3,),
    # fn_raises always raises; the call_function rule swallows the
    # exception and still verifies P4 convergence under the raise path.
    "test.target.fn_raises": (),
    # recur_raise(2) recurses to depth 2 then raises at the base case;
    # call_function swallows the resulting ValueError. Small depth keeps
    # the stateful machine cheap.
    "test.target.recur_raise": (2,),
}

# Drift guard: every specifier in the strategy pool must have an entry in
# the call-args map. If you add a new target to _SPECIFIER_POOL, also add
# its args to _CALL_ARGS_BY_SPECIFIER below.
assert set(_CALL_ARGS_BY_SPECIFIER.keys()) == set(_SPECIFIER_POOL), (
    f"specifier drift between _CALL_ARGS_BY_SPECIFIER and _SPECIFIER_POOL: "
    f"map={set(_CALL_ARGS_BY_SPECIFIER)}, pool={set(_SPECIFIER_POOL)}"
)


def _resolve_target_or_none(specifier: str):
    """Resolve a specifier to its current live callable, or None.

    Used inside the stateful machine's call_function rule to look up the
    function object whose ``__posthog_decorator`` attribute we want to
    check after the call. We use manager.resolve_target so the lookup
    semantics match production exactly.
    """
    return manager.resolve_target(specifier)


def _any_qualname_probed_in_index(qualname: str) -> bool:
    """True iff any entry/exit/line slot for ``qualname`` carries probes.

    Mirrors instrumentation._any_probes_for but reads through the test's
    own snapshot of _PROBE_INDEX so the assertion is independent of the
    production helper under test.
    """
    index = instr._PROBE_INDEX
    return bool(
        index.get((qualname, "entry"))
        or index.get((qualname, "exit"))
        or index.get((qualname, "line"))
    )


class RegistryMachine(RuleBasedStateMachine):
    """Stateful property test: install / uninstall / update + invariants.

    The bundle stores ``program.id`` strings, NOT ``Program`` objects.
    Hogtrace ``Program`` is a PyO3 wrapper that may not be deepcopy-
    friendly and Hypothesis deepcopies bundle contents during shrinking.
    Storing ids only sidesteps the problem entirely — we re-fetch the
    live program from ``_INSTALLED_PROGRAMS`` inside each rule body.

    Phase 4 extension: ``call_function`` rule plus per-step P4 assertion
    ("wrapper IFF probes for this qualname"). Because the wrapper's probe
    path needs an active hogtrace request scope (``get_store()`` returns
    None outside one and probes silently skip), the machine enters a
    request scope in ``__init__`` and exits it in ``teardown``. Using one
    scope per example is sufficient — probes fire normally and the scope
    is torn down between Hypothesis examples.
    """

    program_ids = Bundle("program_ids")

    def __init__(self):
        super().__init__()
        # Mirror of the real registry: id -> Program. Updated in lockstep
        # with every install/uninstall/update rule so the invariant check
        # can spot divergence.
        self._model: dict = {}
        # Hypothesis runs many examples per pytest case; drain anything
        # left over from a previous round.
        _drain_registry()

        # Enter a hogtrace request scope so call_function's wrapped-function
        # invocations actually fire probes (get_store() returns the scope's
        # store, not None). new_context() returns a context manager; we
        # invoke __enter__ here and __exit__ in teardown.
        self._ctx = new_context()
        self._ctx.__enter__()

    @rule(target=program_ids, program=programs_strategy())
    def install(self, program):
        # install_program is defined to overwrite a same-id install; the
        # model mirrors that behavior with a plain dict assignment.
        manager.install_program(program)
        self._model[program.id] = program
        return program.id

    @rule(program_id=consumes(program_ids))
    def uninstall(self, program_id):
        manager.uninstall_program(program_id)
        self._model.pop(program_id, None)

    @rule(target=program_ids, program=programs_strategy())
    def update(self, program):
        # update_program == uninstall(program.id) + install(program).
        # If the id was never installed, uninstall is a silent no-op and
        # install adds it — net effect is identical to a fresh install.
        manager.update_program(program)
        self._model[program.id] = program
        return program.id

    @rule(specifier=st.sampled_from(_SPECIFIER_POOL))
    def call_function(self, specifier):
        """Call a target function once; assert wrapped IFF probed for it.

        This is the P4 convergence probe. After the call:
          - If probes exist for ``specifier`` in _PROBE_INDEX, the wrapper
            must still be in place (``__posthog_decorator`` present).
          - If no probes exist for ``specifier``, the call must have
            triggered self-uninstall (``__posthog_decorator`` absent AND
            ``__code__`` restored to the original).

        The assertion runs INSIDE the rule rather than as a global
        ``@invariant`` because the "wrapper still in place but registry
        empty" state is legal BETWEEN an uninstall rule and the next
        call_function rule. Only after the call do we expect convergence.
        """
        fn = _resolve_target_or_none(specifier)
        if fn is None:
            # Specifier doesn't resolve (shouldn't happen for fixed pool,
            # but guard anyway so the rule can't blow up the machine).
            return

        # Snapshot original code if the function isn't currently wrapped,
        # so we can check __code__ is the original after a self-uninstall.
        # When wrapped, fn.__code__ is the redirector; the original code
        # is stored on the decorator.
        dec_before = getattr(fn, "__posthog_decorator", None)
        original_code = (
            dec_before.original_code if dec_before is not None else fn.__code__
        )

        args = _CALL_ARGS_BY_SPECIFIER[specifier]
        try:
            fn(*args)
        except Exception:
            # Probe path is allowed to log+swallow; user-code exceptions
            # from target functions (none of ours raise on these args, but
            # belt-and-suspenders) propagate through the wrapper and we
            # catch them here so the machine keeps marching.
            pass

        # P4 convergence: wrapper present IFF probes exist for this qualname.
        has_attr = hasattr(fn, "__posthog_decorator")
        has_probes = _any_qualname_probed_in_index(specifier)
        assert has_attr == has_probes, (
            f"P4 convergence violated after calling {specifier}: "
            f"hasattr(__posthog_decorator)={has_attr}, "
            f"has_probes_in_index={has_probes}. Wrapper must exist IFF probes do."
        )

        # And on the false-side, __code__ must be back to the original.
        if not has_attr:
            assert fn.__code__ is original_code, (
                f"P4 convergence: after self-cleanup for {specifier}, "
                f"__code__ must be the original code object"
            )

    @rule(target=program_ids, existing_id=program_ids, program=programs_strategy())
    def install_overwriting(self, existing_id, program):
        # Exercises the same-id collision path that the random-UUID
        # strategy in strategies.programs() would otherwise virtually never
        # hit. ``existing_id`` is a non-consuming read of the bundle (no
        # ``consumes(...)`` wrap) — its value flows back into the bundle
        # via ``target=program_ids`` so the id stays drawable by other
        # rules. We re-package the freshly-generated program with that
        # existing id so install_program takes the overwrite branch.
        forged = ht_package(existing_id, program.program_bytecode)
        manager.install_program(forged)
        # Mirror in the model: same id, new probes -> dict overwrite.
        self._model[existing_id] = forged
        return existing_id

    @invariant()
    def registry_consistent(self):
        # P2: the set of installed program ids matches the model exactly.
        assert set(instr._INSTALLED_PROGRAMS.keys()) == set(self._model.keys()), (
            f"registry diverged from model: "
            f"registry={set(instr._INSTALLED_PROGRAMS.keys())} "
            f"model={set(self._model.keys())}"
        )

    @invariant()
    def index_consistent(self):
        # P3: every (program, probe) pair appearing anywhere in the
        # index belongs to a program currently in the registry.
        for (qualname, kind), pairs in instr._PROBE_INDEX.items():
            for program, probe in pairs:
                assert program.id in instr._INSTALLED_PROGRAMS, (
                    f"_PROBE_INDEX[({qualname!r}, {kind!r})] references "
                    f"program {program.id!r} not in _INSTALLED_PROGRAMS"
                )

    @invariant()
    def all_installed_probes_in_index(self):
        """Converse of index_consistent: every probe of every installed
        program must be reflected in _PROBE_INDEX[(qualname, target)].

        A bug in _rebuild_probe_index that silently dropped probes from
        one program would pass the other two invariants — this catches it.
        """
        for program in instr._INSTALLED_PROGRAMS.values():
            for probe in program.probes:
                key = (probe.spec.specifier, probe.spec.target)
                pairs = instr._PROBE_INDEX.get(key, ())
                ids_in_slot = frozenset((p.id, pr.id) for p, pr in pairs)
                assert (program.id, probe.id) in ids_in_slot, (
                    f"probe {probe.id} of program {program.id} not in "
                    f"_PROBE_INDEX[{key}]; slot had {ids_in_slot}"
                )

    def teardown(self):
        # Run between Hypothesis examples; the autouse ``reset_state``
        # fixture only fires between pytest test cases. Without this,
        # state leaks across rounds and the very first invariant check
        # of round N+1 can fail on round N's residue.
        _drain_registry()
        # Exit the hogtrace request scope set up in __init__. Swallow
        # any exception to keep teardown idempotent and resilient.
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass


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


def _normalized_index() -> Dict[Tuple[str, str], FrozenSet[Tuple[str, str]]]:
    """Map each ``_PROBE_INDEX`` slot to a frozenset of ``(program.id, probe.id)``.

    Strips order-of-iteration noise so two semantically-equivalent indexes
    compare equal. For P5 specifically we want set-equality on slot contents
    — the stronger "order-stable within a slot" property is a separate
    assertion and outside Phase 5's scope.
    """
    return {
        key: frozenset((p.id, pr.id) for p, pr in pairs)
        for key, pairs in instr._PROBE_INDEX.items()
    }


def _normalize_from_programs(
    programs: Iterable[Any],
) -> Dict[Tuple[str, str], FrozenSet[Tuple[str, str]]]:
    """Compute the normalized index that ``_rebuild_probe_index`` would produce
    given just an iterable of Programs (no reliance on the live registry).

    Used by the optional ``index_matches_model_rebuild`` invariant to assert
    that the actual ``_PROBE_INDEX`` agrees with a fresh rebuild from the
    test's own model dict, regardless of operation history.
    """
    out: Dict[Tuple[str, str], set] = {}
    for program in programs:
        for probe in program.probes:
            key = (probe.spec.specifier, probe.spec.target)
            out.setdefault(key, set()).add((program.id, probe.id))
    return {key: frozenset(pairs) for key, pairs in out.items()}


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
    program_set = data.draw(
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


# Optional stronger invariant on the existing stateful machine:
# the actual ``_PROBE_INDEX`` must equal what a fresh rebuild from the
# model dict would produce. This is strictly stronger than the existing
# ``index_consistent`` + ``all_installed_probes_in_index`` invariants
# because it also catches a divergent slot that's STRUCTURALLY present
# but holds an unexpected ``(program_id, probe_id)`` set.
def _index_matches_model_rebuild_invariant(self):
    """Invariant: actual _PROBE_INDEX == rebuild from the model.

    Captures order-independence (P5) by asserting that whatever sequence
    of operations the stateful machine just performed, the result equals
    a from-scratch rebuild over the test's mirror of the registry.
    """
    expected = _normalize_from_programs(self._model.values())
    actual = _normalized_index()
    assert expected == actual, (
        f"_PROBE_INDEX diverged from model rebuild. "
        f"expected={expected!r} actual={actual!r}"
    )


# Attach as an invariant on the class. Using @invariant() as a decorator
# inline on the method would have been cleaner but the machine is already
# defined above; this avoids duplicating it.
RegistryMachine.index_matches_model_rebuild = invariant()(
    _index_matches_model_rebuild_invariant
)


# Hypothesis defaults are usually fine; we bump max_examples a bit because
# the stateful machine's individual examples are cheap and we want decent
# coverage of install/uninstall/update interleavings.
RegistryMachine.TestCase.settings = hyp_settings(
    max_examples=50,
    stateful_step_count=20,
    deadline=None,
)

TestRegistry = RegistryMachine.TestCase


# ---------------------------------------------------------------------------
# Phase 6 — Recursion safety (P6)
# ---------------------------------------------------------------------------
#
# Property: for a function F that calls itself N times within one outer call,
# ``_enqueue_message`` fires exactly ``N * (entry_count + exit_count)`` times.
# No deadlock, no missed probes, no double-firing.
#
# The frame-stack handling in ``InstrumentationDecorator.__call__`` is what
# makes recursion work — each call pushes its own frame, and the
# ``previous_frame_top`` check distinguishes "we pushed a new frame this call"
# from "we didn't." These tests pin that behavior under increasing depth and
# under an exception mid-recursion.
#
# Invocation count for ``test.target.fact``:
#   fact(N) when N <= 1 returns 1 directly (base case) -> 1 invocation.
#   fact(N) when N >= 2 recurses to fact(N-1) -> N invocations total.
#   So total invocations = max(N, 1).


def _expected_fact_invocations(n: int) -> int:
    """Total invocations of fact() for an outer call of fact(n).

    Matches the recurrence in ``test/target.py::fact``: base case at n <= 1
    short-circuits without recursing, so fact(0) and fact(1) each count as
    a single invocation. fact(N) for N >= 2 produces N total invocations
    (N, N-1, ..., 1).
    """
    return max(n, 1)


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

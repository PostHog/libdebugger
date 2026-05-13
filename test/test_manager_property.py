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
from typing import Any, Callable, Tuple

import hypothesis.strategies as st
import pytest
from hogtrace.context import new_context
from hogtrace.vm import compile as ht_compile, package as ht_package
from hypothesis import given, settings

import libdebugger.instrumentation as instr
from libdebugger.instrumentation import InstrumentationDecorator


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
# Stateful machine — Hypothesis explores arbitrary install/uninstall/update
# sequences and checks the P2/P3 invariants after every step.
# ---------------------------------------------------------------------------


from hypothesis import settings as hyp_settings  # noqa: E402
from hypothesis.stateful import (  # noqa: E402
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    rule,
)

from test.strategies import programs as programs_strategy  # noqa: E402

import libdebugger.manager as manager  # noqa: E402


def _drain_registry():
    """Tear down everything in the registry plus any lingering wrappers.

    Used as cross-round cleanup inside the stateful machine — Hypothesis
    runs many examples within a single pytest invocation and the
    ``reset_state`` fixture only fires between pytest test cases, not
    between Hypothesis examples.
    """
    for pid in list(instr._INSTALLED_PROGRAMS):
        try:
            manager.uninstall_program(pid)
        except Exception:
            pass

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


class RegistryMachine(RuleBasedStateMachine):
    """Stateful property test: install / uninstall / update + invariants.

    The bundle stores ``program.id`` strings, NOT ``Program`` objects.
    Hogtrace ``Program`` is a PyO3 wrapper that may not be deepcopy-
    friendly and Hypothesis deepcopies bundle contents during shrinking.
    Storing ids only sidesteps the problem entirely — we re-fetch the
    live program from ``_INSTALLED_PROGRAMS`` inside each rule body.
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

    def teardown(self):
        # Run between Hypothesis examples; the autouse ``reset_state``
        # fixture only fires between pytest test cases. Without this,
        # state leaks across rounds and the very first invariant check
        # of round N+1 can fail on round N's residue.
        _drain_registry()


# Hypothesis defaults are usually fine; we bump max_examples a bit because
# the stateful machine's individual examples are cheap and we want decent
# coverage of install/uninstall/update interleavings.
RegistryMachine.TestCase.settings = hyp_settings(
    max_examples=50,
    stateful_step_count=20,
    deadline=None,
)

TestRegistry = RegistryMachine.TestCase


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

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
    wrapped function and assert equality, then unwrap and call again.
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

            # 3. Wrapped call equals expected.
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


def test_module_globals_present():
    """Phase 1 production-code invariant: registry globals exist as empty dicts."""
    assert hasattr(instr, "_PROBE_INDEX")
    assert hasattr(instr, "_INSTALLED_PROGRAMS")
    assert hasattr(instr, "_LOCK")
    # Both registries start empty.
    assert instr._PROBE_INDEX == {}
    assert instr._INSTALLED_PROGRAMS == {}

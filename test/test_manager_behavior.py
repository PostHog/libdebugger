"""
Phase 1 — Behavior preservation (P7).

An instrumented function returns the same value (or raises the same
exception) as its uninstrumented counterpart, modulo probe side effects.
With no probes installed at all, wrapping and unwrapping a function must
be a perfect no-op observable to callers.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings

import libdebugger.instrumentation as instr
from libdebugger.instrumentation import InstrumentationDecorator
from test._manager_helpers import TARGETS, _unwrap, target_mod


def test_imports_clean():
    """Sanity check that the production modules import without error."""
    import libdebugger.instrumentation  # noqa: F401
    import libdebugger.manager  # noqa: F401
    from test import strategies, target  # noqa: F401


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

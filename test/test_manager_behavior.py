"""
Phase 1 — Behavior preservation (P7).

An instrumented function returns the same value (or raises the same
exception) as its uninstrumented counterpart, modulo probe side effects.
With ``sys.monitoring``-based dispatch wrapping is a literal no-op on
``__code__`` — the events fire, our callbacks run, but the user code
runs unchanged.
"""

from __future__ import annotations

import pytest
from hogtrace.context import new_context
from hypothesis import given, settings

import libdebugger.instrumentation as instr
from libdebugger.manager import install_program, uninstall_program
from test._manager_helpers import TARGETS, _build_program, target_mod


def test_imports_clean():
    """The production modules import without error."""
    import libdebugger.instrumentation  # noqa: F401
    import libdebugger.manager  # noqa: F401
    from test import strategies, target  # noqa: F401


@pytest.mark.parametrize(
    "fn_getter,qualname,args_strategy",
    TARGETS,
    ids=[q for _, q, _ in TARGETS],
)
def test_install_uninstall_preserves_behavior(fn_getter, qualname, args_strategy):
    """For each pool function, install+uninstall is a no-op on return values."""
    fn = fn_getter()
    original_code = fn.__func__.__code__ if hasattr(fn, "__func__") else fn.__code__

    @given(args=args_strategy)
    @settings(max_examples=25, deadline=None)
    def _inner(args):
        expected = fn(*args)

        program = _build_program(
            f"fn:{qualname}:entry {{ }}\nfn:{qualname}:exit {{ }}",
            program_id=f"behavior-{qualname}",
        )
        with new_context():
            install_program(program)
            try:
                assert fn(*args) == expected
                assert fn(*args) == expected
            finally:
                uninstall_program(program.id)

        # __code__ is never mutated — sys.monitoring observes, doesn't rewrite.
        assert (
            fn.__func__.__code__ if hasattr(fn, "__func__") else fn.__code__
        ) is original_code
        assert fn(*args) == expected

    _inner()


def test_install_uninstall_preserves_exception():
    """The wrapped function raises the same exception as the uninstrumented one."""
    program = _build_program(
        "fn:test.target.fn_raises:entry { }\nfn:test.target.fn_raises:exit { }",
        program_id="behavior-raises",
    )

    with new_context():
        install_program(program)
        try:
            with pytest.raises(ValueError, match="boom"):
                target_mod.fn_raises()
            with pytest.raises(ValueError, match="boom"):
                target_mod.fn_raises()
        finally:
            uninstall_program(program.id)

    with pytest.raises(ValueError, match="boom"):
        target_mod.fn_raises()


def test_install_uninstall_method_on_class():
    """Bound methods route through the underlying code object."""
    klass_instance = target_mod.Klass()
    expected_3 = klass_instance.method(3)
    expected_minus_2 = klass_instance.method(-2)

    program = _build_program(
        "fn:test.target.Klass.method:entry { }",
        program_id="behavior-method",
    )

    with new_context():
        install_program(program)
        try:
            assert klass_instance.method(3) == expected_3
            assert klass_instance.method(-2) == expected_minus_2
        finally:
            uninstall_program(program.id)

    assert klass_instance.method(3) == expected_3


def test_marker_cleared_after_uninstall():
    """``__posthog_decorator`` is gone synchronously after the last uninstall.

    Earlier versions deferred cleanup to the next call; ``sys.monitoring``
    lets us disable events at uninstall time so the marker comes off too.
    """
    fn = target_mod.fn_a
    program = _build_program(
        "fn:test.target.fn_a:entry { }",
        program_id="behavior-marker",
    )

    assert not hasattr(fn, "__posthog_decorator")

    with new_context():
        install_program(program)
        assert hasattr(fn, "__posthog_decorator")
        uninstall_program(program.id)

    assert not hasattr(fn, "__posthog_decorator")


def test_module_globals_present():
    """The dispatch state globals exist as empty containers on a fresh import."""
    assert hasattr(instr, "_PROBE_INDEX")
    assert hasattr(instr, "_INSTALLED_PROGRAMS")
    assert hasattr(instr, "_LOCK")
    assert hasattr(instr, "_CODE_TO_QUALNAME")
    assert hasattr(instr, "_MONITORED_CODES")
    assert instr._PROBE_INDEX == {}
    assert instr._INSTALLED_PROGRAMS == {}

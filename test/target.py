"""
Stable target functions that property tests instrument.

This module is intentionally dependency-free. Do NOT import libdebugger or
hogtrace here - tests rely on these functions being plain Python with stable
qualnames so that probe specifiers like ``fn:test.target.fn_a`` resolve
predictably.
"""


def fn_a(x=0):
    """Simple deterministic function used as instrumentation target."""
    a = 1
    b = 2
    return a + b + x


def fn_b(x=0, y=0):
    """Two-argument variant."""
    return x + y


def fn_c(s="hello"):
    """String identity function."""
    return s


def fn_d(items=None):
    """Length-of-list (or 0 for None)."""
    if items is None:
        return 0
    return len(items)


def fn_e():
    """No-arg function returning a constant."""
    return 42


class Klass:
    """Plain class with a method used to verify method instrumentation."""

    def method(self, n=0):
        return n * 2


def fact(n):
    """
    Recursive factorial-ish function.

    The recursion depth is capped at 100 so Hypothesis-generated huge values
    can't blow the stack. The numeric result is therefore NOT actual factorial
    for n > 100; tests should not rely on the result being mathematically
    correct, only on the function being recursive and deterministic.
    """
    if n <= 1:
        return 1
    return n * fact(min(n - 1, 100))


def fn_raises():
    """Always raises ``ValueError("boom")``. Used to exercise exit-probe-on-exception."""
    raise ValueError("boom")

"""
Hypothesis strategies for hogtrace-manager property tests.

These strategies produce inputs for the rewritten manager / instrumentation
layer. They are deliberately conservative on the hogtrace source side: we
stick to ``capture(name=value)`` with literal arguments so that test failures
point at the manager / instrumentation code under test, not at hogtrace
expression edge cases.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hogtrace import Program
from hogtrace.vm import compile as hogtrace_compile, package


# Fully-qualified names of stable functions defined in test/target.py.
# Tests build probe specifiers from these so the qualnames must match the
# actual import path of the target module.
_SPECIFIER_POOL = [
    "test.target.fn_a",
    "test.target.fn_b",
    "test.target.fn_c",
    "test.target.fn_d",
    "test.target.fn_e",
    "test.target.Klass.method",
    "test.target.fact",
    "test.target.fn_raises",
    "test.target.recur_raise",
]


_TARGETS = ("entry", "exit")


@st.composite
def _probe_blocks(draw) -> str:
    """One probe block like ``fn:test.target.fn_a:entry { capture(x=1); }``."""
    spec = draw(st.sampled_from(_SPECIFIER_POOL))
    target = draw(st.sampled_from(_TARGETS))
    # Keep the capture body trivial - we just need a syntactically valid probe
    # so the manager has something to attach. Real capture semantics are
    # exercised by hogtrace's own test suite, not ours.
    value = draw(st.integers(min_value=0, max_value=100))
    return f"fn:{spec}:{target} {{ capture(x={value}); }}"


@st.composite
def programs(draw, probes_max: int = 4) -> Program:
    """
    Build a ``hogtrace.Program``.

    Internally this calls ``hogtrace.vm.compile(source)`` to get a
    ``ProgramBytecode`` and then ``hogtrace.vm.package(id, bytecode)`` to wrap
    it in a ``Program``. The two-step dance matches the spec: ``compile``
    alone does NOT yield a ``Program``.
    """
    n_probes = draw(st.integers(min_value=1, max_value=probes_max))
    blocks = [draw(_probe_blocks()) for _ in range(n_probes)]
    source = "\n".join(blocks)

    program_id = draw(st.uuids().map(str))

    bytecode = hogtrace_compile(source)
    return package(program_id, bytecode)


def program_lists(max_size: int = 5) -> st.SearchStrategy[list[Program]]:
    """Lists of programs with distinct IDs.

    May produce an empty list (``min_size`` defaults to 0).
    """
    return st.lists(
        programs(),
        max_size=max_size,
        unique_by=lambda p: p.id,
    )

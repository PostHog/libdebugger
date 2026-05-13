"""
Meta-tests for ``test/strategies.py``.

These don't exercise libdebugger itself - they just confirm the Hypothesis
strategies produce well-typed values so later phases can rely on them.
"""

import importlib

from hogtrace import Program
from hypothesis import given, settings

from test import strategies


def test_specifiers_pool_resolves():
    """Every specifier in the pool must point at a real attribute."""
    for qualname in strategies._SPECIFIER_POOL:
        # qualname is like "test.target.fn_a" or "test.target.Klass.method".
        parts = qualname.split(".")
        # Walk module path until import fails, then attribute-walk the rest.
        module = None
        attr_parts: list[str] = []
        for i in range(len(parts), 0, -1):
            try:
                module = importlib.import_module(".".join(parts[:i]))
                attr_parts = parts[i:]
                break
            except ImportError:
                continue
        assert module is not None, f"could not import any prefix of {qualname}"

        obj = module
        for attr in attr_parts:
            assert hasattr(obj, attr), (
                f"qualname {qualname!r} broke at {attr!r} (obj={obj!r})"
            )
            obj = getattr(obj, attr)


@given(strategies.programs())
@settings(max_examples=5, deadline=None)
def test_programs_strategy_produces_program(example):
    """``strategies.programs()`` yields ``hogtrace.Program`` instances."""
    assert isinstance(example, Program), f"got {type(example).__name__}"
    assert example.id, "program should have a non-empty id"
    assert len(example.probes) >= 1, "program should have at least one probe"


@given(strategies.program_lists(max_size=3))
@settings(max_examples=5, deadline=None)
def test_program_lists_have_unique_ids(progs):
    """``program_lists`` enforces ID uniqueness via ``unique_by``."""
    ids = [p.id for p in progs]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"
    for p in progs:
        assert isinstance(p, Program)

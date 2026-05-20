"""
Shared helpers for the test_manager_*.py suite.

These were extracted from the original ``test_manager_property.py`` so each
per-phase test file can import what it needs without duplicating definitions.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, FrozenSet, Iterable, Tuple

import hypothesis.strategies as st
from hogtrace.vm import compile as ht_compile, package as ht_package

import libdebugger.instrumentation as instr
import libdebugger.manager as manager
from test.strategies import _SPECIFIER_POOL


# ---------------------------------------------------------------------------
# Shared target module + per-function args strategies (used by Phase 1 + Phase 6).
# ---------------------------------------------------------------------------

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
    """Drop the ``__posthog_decorator`` sentinel a test may have set up.

    With ``sys.monitoring``-based dispatch this is just a ``delattr``;
    the marker is a flag, not a bytecode mutation. Kept as a helper so
    older test patterns that need explicit cleanup keep working.
    """
    try:
        delattr(fn, "__posthog_decorator")
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# Program builder used by every probe-firing / registry test.
# ---------------------------------------------------------------------------


def _build_program(source: str, program_id: str = "test-prog"):
    """Compile a single hogtrace source snippet into a packaged Program."""
    return ht_package(program_id, ht_compile(source))


# ---------------------------------------------------------------------------
# Stateful machine helpers (Phase 3-5).
# ---------------------------------------------------------------------------


def _drain_registry() -> None:
    """Tear down every installed program plus any leftover markers.

    Used as cross-round cleanup inside the stateful machine — Hypothesis
    runs many examples within a single pytest invocation and the
    ``reset_state`` fixture only fires between pytest test cases, not
    between Hypothesis examples. With synchronous marker cleanup the
    second loop is just defensive belt-and-suspenders.
    """
    for pid in list(instr._INSTALLED_PROGRAMS):
        manager.uninstall_program(pid)

    for _name, obj in list(vars(target_mod).items()):
        if hasattr(obj, "__posthog_decorator"):
            try:
                delattr(obj, "__posthog_decorator")
            except AttributeError:
                pass
        if isinstance(obj, type):
            for _mname, mobj in list(vars(obj).items()):
                if hasattr(mobj, "__posthog_decorator"):
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


# ---------------------------------------------------------------------------
# Phase 5 — Order-independence helpers.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 6 — recursion invocation counter.
# ---------------------------------------------------------------------------


def _expected_fact_invocations(n: int) -> int:
    """Total invocations of fact() for an outer call of fact(n).

    Matches the recurrence in ``test/target.py::fact``: base case at n <= 1
    short-circuits without recursing, so fact(0) and fact(1) each count as
    a single invocation. fact(N) for N >= 2 produces N total invocations
    (N, N-1, ..., 1).
    """
    return max(n, 1)


__all__ = [
    "TARGETS",
    "_CALL_ARGS_BY_SPECIFIER",
    "_any_qualname_probed_in_index",
    "_build_program",
    "_drain_registry",
    "_expected_fact_invocations",
    "_normalize_from_programs",
    "_normalized_index",
    "_resolve_target_or_none",
    "_unwrap",
    "instr",
    "manager",
    "target_mod",
]

"""
Stateful Hypothesis machine exploring arbitrary install / uninstall / update
sequences plus the P2 / P3 / P4 / P5 invariants asserted after every step.

Lives in its own file so the per-phase test files (behavior, probe-firing,
self-cleanup, recursion) stay focused on hand-written cases. The machine
itself is the more expensive but also the more thorough property test.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hogtrace.context import new_context
from hogtrace.vm import package as ht_package
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
from test._manager_helpers import (
    _CALL_ARGS_BY_SPECIFIER,
    _any_qualname_probed_in_index,
    _drain_registry,
    _expected_code_probe_index_from_programs,
    _expected_monitored_codes_from_programs,
    _normalize_from_programs,
    _normalized_code_probe_index,
    _normalized_index,
    _resolve_target_or_none,
)
from test.strategies import _SPECIFIER_POOL, programs as programs_strategy


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
        """Call a target function once; assert instrumented IFF probes exist for it.

        This is the P4 convergence probe. With sys.monitoring-based
        dispatch the cleanup happens synchronously inside
        ``uninstall_program``, so the convergence holds after any
        install/uninstall, with or without a call.
        """
        fn = _resolve_target_or_none(specifier)
        if fn is None:
            return

        original_code = fn.__func__.__code__ if hasattr(fn, "__func__") else fn.__code__

        args = _CALL_ARGS_BY_SPECIFIER[specifier]
        try:
            fn(*args)
        except Exception:
            # User-code exceptions from target functions propagate; we
            # catch them so the machine keeps marching.
            pass

        # P4 convergence: code monitored IFF probes exist for this qualname.
        has_monitoring = instr.is_instrumented(fn)
        has_probes = _any_qualname_probed_in_index(specifier)
        assert has_monitoring == has_probes, (
            f"P4 convergence violated after calling {specifier}: "
            f"is_instrumented={has_monitoring}, "
            f"has_probes_in_index={has_probes}. Both must agree."
        )

        # __code__ is never mutated under sys.monitoring.
        if not has_monitoring:
            assert (
                fn.__func__.__code__ if hasattr(fn, "__func__") else fn.__code__
            ) is original_code, (
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

    @rule(target=program_ids, existing_id=program_ids, program=programs_strategy())
    def update_existing(self, existing_id, program):
        # Sibling of install_overwriting that exercises the public
        # ``update_program`` entrypoint on an in-use id. The random-UUID
        # strategy makes update_program against a known id effectively
        # impossible to hit otherwise; without this rule, the state
        # machine's random walk almost never touches the same-id update
        # path the manager treats as the canonical "swap the probe set"
        # operation.
        forged = ht_package(existing_id, program.program_bytecode)
        manager.update_program(forged)
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

    @invariant()
    def index_matches_model_rebuild(self):
        """Phase 5 invariant: actual _PROBE_INDEX == rebuild from the model.

        Strictly stronger than the existing ``index_consistent`` +
        ``all_installed_probes_in_index`` invariants — it also catches a
        slot that's structurally present but holds an unexpected
        ``(program_id, probe_id)`` set, which is what order-dependence
        would look like in practice.
        """
        expected = _normalize_from_programs(self._model.values())
        actual = _normalized_index()
        assert expected == actual, (
            f"_PROBE_INDEX diverged from model rebuild. "
            f"expected={expected!r} actual={actual!r}"
        )

    @invariant()
    def code_probe_index_matches_model_rebuild(self):
        """Actual ``_CODE_PROBE_INDEX`` == code-keyed rebuild from the model.

        ``_PROBE_INDEX`` is the qualname-keyed view; ``_CODE_PROBE_INDEX``
        is what the ``sys.monitoring`` callbacks actually read. A bug that
        only corrupted the dispatch table (aliased-code dropping a probe,
        an unresolvable specifier polluting the dispatch path, line probes
        leaking through) would pass the qualname-keyed invariants but
        break this one.
        """
        expected = _expected_code_probe_index_from_programs(self._model.values())
        actual = _normalized_code_probe_index()
        assert expected == actual, (
            f"_CODE_PROBE_INDEX diverged from model rebuild. "
            f"expected={expected!r} actual={actual!r}"
        )

    @invariant()
    def monitored_codes_matches_model_rebuild(self):
        """Actual ``_MONITORED_CODES`` == event-mask rebuild from the model.

        Catches drift between the dispatch table and what
        ``sys.monitoring`` actually has enabled — e.g. a code that left
        the dispatch table but never got ``set_local_events(..., 0)``, or
        an entry-only probe enabling exit events because the kind set was
        computed incorrectly.
        """
        expected = _expected_monitored_codes_from_programs(self._model.values())
        actual = dict(instr._MONITORED_CODES)
        assert expected == actual, (
            f"_MONITORED_CODES diverged from model rebuild. "
            f"expected={expected!r} actual={actual!r}"
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


# Hypothesis defaults are usually fine; we bump max_examples a bit because
# the stateful machine's individual examples are cheap and we want decent
# coverage of install/uninstall/update interleavings.
RegistryMachine.TestCase.settings = hyp_settings(
    max_examples=50,
    stateful_step_count=20,
    deadline=None,
)

TestRegistry = RegistryMachine.TestCase

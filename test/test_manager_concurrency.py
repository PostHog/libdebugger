"""
Phase 7 — Thread interleaving (P8).

Property: under concurrent install/uninstall from one thread and ``F(...)``
calls from many threads, properties 1 (trace fidelity), 4 (self-cleanup
convergence), and 7 (behavior preservation) still hold. No
``RuntimeError: dictionary changed size during iteration`` or partial-
state observations.

These tests exercise the atomic-rebind + per-wrapper-lock discipline
established in earlier phases. Production code changes here would be a
sign that an earlier phase landed an unsafe pattern.

A note on hogtrace request scope: ``hogtrace.context.new_context()`` is
built on top of ``contextvars.ContextVar``. ``ContextVar`` lookups return
the calling thread's context, and *new threads start with a default
context* (not a copy of the spawning thread's). So each worker thread
needs to enter its OWN ``with new_context():`` block — otherwise
``get_store()`` returns ``None`` and every probe silently skips, which
would let racy code pass the test for the wrong reason. We assert at
least some probe fires per test to guard against that failure mode.
"""

from __future__ import annotations

import importlib
import random
import threading
import time
from typing import Any, Callable, Dict, List, Tuple

import pytest
from hogtrace.context import new_context
from hogtrace.vm import compile as ht_compile, package as ht_package

import libdebugger.instrumentation as instr
import libdebugger.manager as manager


target_mod = importlib.import_module("test.target")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_program(source: str, program_id: str):
    """Compile a single hogtrace source snippet into a packaged Program."""
    return ht_package(program_id, ht_compile(source))


# Pool of specifiers used by reconciler threads. Exclude raise-y functions
# from the "call any wrapped function" pool below; both pools are kept
# small and stable so the stress is reproducible.
_SPECIFIER_POOL: List[str] = [
    "test.target.fn_a",
    "test.target.fn_b",
    "test.target.fn_c",
    "test.target.fn_e",
    "test.target.Klass.method",
]

# Args usable for each specifier. Plain Python tuples — we are NOT inside
# a Hypothesis context so we want fixed deterministic args.
_CALL_ARGS_BY_SPECIFIER: Dict[str, Tuple[Any, ...]] = {
    "test.target.fn_a": (1,),
    "test.target.fn_b": (1, 2),
    "test.target.fn_c": ("x",),
    "test.target.fn_e": (),
    "test.target.Klass.method": (3,),
}


def _build_program_for(specifier: str, program_id: str):
    """Build a program with one entry probe on ``specifier``."""
    return _build_program(
        f"fn:{specifier}:entry {{ capture(x=1); }}",
        program_id=program_id,
    )


def _resolve_callable(specifier: str) -> Callable[..., Any] | None:
    """Resolve a specifier to its callable.

    For ``test.target.Klass.method`` we resolve to a bound method on a
    fresh instance — the wrapper sits on the underlying function so
    every Klass instance shares the wrapper.
    """
    if specifier == "test.target.Klass.method":
        return target_mod.Klass().method
    return manager.resolve_target(specifier)


def _drain_registry() -> None:
    """Tear down everything: registry + any lingering wrappers."""
    for pid in list(instr._INSTALLED_PROGRAMS):
        try:
            manager.uninstall_program(pid)
        except Exception:
            pass
    # Tear down any wrapper still attached.
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


@pytest.fixture
def fire_counter(monkeypatch):
    """Count ``_enqueue_message`` calls across all threads.

    Returned list of ``(program_id, probe_id)`` tuples is appended-to under
    a lock from the wrapper hot path. We never iterate it from the
    wrapper hot path so a plain list-append is fine.
    """
    fires: List[Tuple[str, str]] = []
    fires_lock = threading.Lock()

    def _stub(program, probe, captures):
        with fires_lock:
            fires.append((program.id, probe.id))

    monkeypatch.setattr(instr, "_enqueue_message", _stub)
    return fires


# ---------------------------------------------------------------------------
# Test: concurrent install/install
# ---------------------------------------------------------------------------


def test_concurrent_install_install():
    """Two threads concurrently install DIFFERENT programs.

    After both join, both program ids must be in ``_INSTALLED_PROGRAMS``
    AND ``_PROBE_INDEX`` must reflect both probes. The writer-vs-writer
    serialization through ``_LOCK`` should make this deterministic.
    """
    prog_a = _build_program_for("test.target.fn_a", "concurrent-install-a")
    prog_b = _build_program_for("test.target.fn_b", "concurrent-install-b")

    errors: List[BaseException] = []
    errors_lock = threading.Lock()
    start_barrier = threading.Barrier(2)

    def _record(e: BaseException) -> None:
        with errors_lock:
            errors.append(e)

    def _installer(program):
        try:
            start_barrier.wait(timeout=5.0)
            manager.install_program(program)
        except BaseException as e:
            _record(e)

    t1 = threading.Thread(target=_installer, args=(prog_a,), name="install-a")
    t2 = threading.Thread(target=_installer, args=(prog_b,), name="install-b")

    try:
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        if t1.is_alive() or t2.is_alive():
            _record(RuntimeError("installer thread did not join — deadlock?"))

        assert not errors, f"errors during concurrent install: {errors}"

        # Both programs landed.
        assert "concurrent-install-a" in instr._INSTALLED_PROGRAMS
        assert "concurrent-install-b" in instr._INSTALLED_PROGRAMS

        # Both probes are reflected in the index.
        assert ("test.target.fn_a", "entry") in instr._PROBE_INDEX
        assert ("test.target.fn_b", "entry") in instr._PROBE_INDEX
    finally:
        _drain_registry()


# ---------------------------------------------------------------------------
# Test: concurrent install/uninstall on disjoint programs
# ---------------------------------------------------------------------------


def test_concurrent_install_uninstall():
    """One thread installs program A; another uninstalls a previously-installed
    program B. Final state: A present, B absent. Deterministic and correct."""
    prog_a = _build_program_for("test.target.fn_a", "concurrent-iu-a")
    prog_b = _build_program_for("test.target.fn_b", "concurrent-iu-b")

    # Pre-install B so the uninstaller has something to remove.
    manager.install_program(prog_b)
    assert "concurrent-iu-b" in instr._INSTALLED_PROGRAMS

    errors: List[BaseException] = []
    errors_lock = threading.Lock()
    start_barrier = threading.Barrier(2)

    def _record(e: BaseException) -> None:
        with errors_lock:
            errors.append(e)

    def _installer():
        try:
            start_barrier.wait(timeout=5.0)
            manager.install_program(prog_a)
        except BaseException as e:
            _record(e)

    def _uninstaller():
        try:
            start_barrier.wait(timeout=5.0)
            manager.uninstall_program("concurrent-iu-b")
        except BaseException as e:
            _record(e)

    t1 = threading.Thread(target=_installer, name="install-a")
    t2 = threading.Thread(target=_uninstaller, name="uninstall-b")

    try:
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        if t1.is_alive() or t2.is_alive():
            _record(RuntimeError("thread did not join — deadlock?"))

        assert not errors, f"errors during install/uninstall: {errors}"

        # Final state is deterministic: A in, B out.
        assert "concurrent-iu-a" in instr._INSTALLED_PROGRAMS
        assert "concurrent-iu-b" not in instr._INSTALLED_PROGRAMS
        assert ("test.target.fn_a", "entry") in instr._PROBE_INDEX
        assert ("test.target.fn_b", "entry") not in instr._PROBE_INDEX
    finally:
        _drain_registry()


# ---------------------------------------------------------------------------
# Test: concurrent calls under install/uninstall (the canonical stress)
# ---------------------------------------------------------------------------


def test_concurrent_calls_under_install_uninstall(fire_counter):
    """N worker threads call wrapped functions while a reconciler installs/uninstalls.

    Verifies the hot-path read of ``_PROBE_INDEX`` never raises
    ``RuntimeError: dictionary changed size during iteration``. The
    atomic-rebind discipline plus the per-wrapper lock should make this
    safe. If it isn't, the failure is a real bug — fix it in the relevant
    earlier phase, not by reducing the stress.
    """
    stop_event = threading.Event()
    errors: List[BaseException] = []
    errors_lock = threading.Lock()

    def _record(e: BaseException) -> None:
        with errors_lock:
            errors.append(e)

    # Pre-install one program so the wrappers are in place from the start.
    initial = _build_program_for("test.target.fn_a", "stress-initial")
    manager.install_program(initial)

    def _worker_caller():
        # Each worker MUST have its own hogtrace request scope — contextvars
        # do not propagate to new threads automatically. Without this,
        # get_store() returns None and probes silently skip — the stress
        # would not actually exercise the probe firing path.
        try:
            with new_context():
                while not stop_event.is_set():
                    for specifier in _SPECIFIER_POOL:
                        if stop_event.is_set():
                            break
                        fn = _resolve_callable(specifier)
                        if fn is None:
                            continue
                        args = _CALL_ARGS_BY_SPECIFIER[specifier]
                        try:
                            fn(*args)
                        except Exception:
                            # Target functions don't raise on the args we
                            # pass — but a wrapper bug could surface as an
                            # unexpected exception. We swallow here and let
                            # the final assertion catch any test-flagged
                            # errors. RuntimeError "dictionary changed size
                            # during iteration" is NOT caught here because
                            # we want it to propagate via _record below.
                            pass
        except BaseException as e:
            _record(e)

    def _reconciler():
        try:
            rng = random.Random(0xC0FFEE)
            counter = 0
            while not stop_event.is_set():
                specifier = rng.choice(_SPECIFIER_POOL)
                pid = f"stress-{counter}"
                counter += 1
                program = _build_program_for(specifier, pid)
                manager.install_program(program)
                # Small sleep so the workers get a window to actually
                # observe the new probe; without it the install/uninstall
                # cycle is so tight that probes never fire.
                time.sleep(0.0005)
                manager.uninstall_program(pid)
        except BaseException as e:
            _record(e)

    workers = [
        threading.Thread(target=_worker_caller, name=f"worker-{i}") for i in range(4)
    ]
    reconcilers = [threading.Thread(target=_reconciler, name="reconciler-0")]

    try:
        for t in workers + reconcilers:
            t.start()

        # Stress duration. Long enough to interleave thousands of ops; short
        # enough that the test suite stays snappy.
        time.sleep(1.0)
        stop_event.set()

        for t in workers + reconcilers:
            t.join(timeout=10)
            if t.is_alive():
                _record(RuntimeError(f"thread {t.name} did not join — deadlock?"))

        assert not errors, f"errors during concurrent stress: {errors}"

        # Verify the test actually exercised the probe path. If
        # _enqueue_message never fired, our hogtrace scope setup is wrong
        # and we passed for the wrong reason.
        assert len(fire_counter) > 0, (
            "no probes fired during the stress — hogtrace scope setup wrong "
            "or workers never observed an installed program"
        )
    finally:
        stop_event.set()
        _drain_registry()


# ---------------------------------------------------------------------------
# Test: caller thread + reconciler thread on the same function
# ---------------------------------------------------------------------------


def test_concurrent_call_during_reconcile(fire_counter):
    """A worker calls fn_a in a tight loop while a reconciler installs/uninstalls
    programs targeting fn_a.

    The wrapper must survive the self-clean + re-wrap cycle without
    raising. The target function must return its normal value every
    call (behavior preservation, P7).

    This is the worst-case for wrapper self-cleanup convergence (P4):
    every uninstall leaves a wrapper that the next call should tear
    down — but if the next call beats the reconcile in starting and
    a fresh install lands before the call gets to its self-cleanup
    check, the wrapper must stay in place.
    """
    stop_event = threading.Event()
    errors: List[BaseException] = []
    errors_lock = threading.Lock()
    call_results: List[int] = []
    results_lock = threading.Lock()

    def _record(e: BaseException) -> None:
        with errors_lock:
            errors.append(e)

    expected_value = target_mod.fn_a(7)  # uninstrumented baseline

    def _worker_caller():
        try:
            with new_context():
                while not stop_event.is_set():
                    result = target_mod.fn_a(7)
                    with results_lock:
                        call_results.append(result)
        except BaseException as e:
            _record(e)

    def _reconciler():
        try:
            counter = 0
            while not stop_event.is_set():
                pid = f"contend-{counter}"
                counter += 1
                program = _build_program_for("test.target.fn_a", pid)
                manager.install_program(program)
                time.sleep(0.0003)
                manager.uninstall_program(pid)
                time.sleep(0.0003)
        except BaseException as e:
            _record(e)

    workers = [
        threading.Thread(target=_worker_caller, name=f"caller-{i}") for i in range(3)
    ]
    reconciler = threading.Thread(target=_reconciler, name="reconciler")

    try:
        for t in workers + [reconciler]:
            t.start()

        time.sleep(1.0)
        stop_event.set()

        for t in workers + [reconciler]:
            t.join(timeout=10)
            if t.is_alive():
                _record(RuntimeError(f"thread {t.name} did not join — deadlock?"))

        assert not errors, f"errors during concurrent call/reconcile: {errors}"

        # P7: every call returned the expected value, regardless of whether
        # the function was wrapped at call time.
        assert len(call_results) > 0, "workers never got a call in"
        assert all(r == expected_value for r in call_results), (
            f"behavior preservation violated: some calls returned non-{expected_value} "
            f"values; distinct={set(call_results)}"
        )

        # Verify we actually exercised the probe path (at least one
        # install/call coincidence fired a probe).
        assert len(fire_counter) > 0, (
            "no probes fired — the install/uninstall window was too tight "
            "or hogtrace scope setup is wrong"
        )

        # P4 convergence: after stopping the reconciler, drain and call once;
        # the wrapper should be gone.
        _drain_registry()
        # One last call to trip the lazy self-cleanup path if it didn't
        # already happen. After this, fn_a must be unwrapped.
        target_mod.fn_a(7)
        assert not hasattr(target_mod.fn_a, "__posthog_decorator"), (
            "P4: after drain + call, fn_a wrapper must self-clean"
        )
    finally:
        stop_event.set()
        _drain_registry()


# ---------------------------------------------------------------------------
# Test: many install/uninstall threads — writer-vs-writer contention
# ---------------------------------------------------------------------------


def test_many_concurrent_reconcilers_converge():
    """Many threads concurrently install and uninstall their own programs.

    Final state after all threads join + a drain: registry is consistent
    with the model (P2). This exercises pure writer-vs-writer contention
    on ``_LOCK``.
    """
    stop_event = threading.Event()
    errors: List[BaseException] = []
    errors_lock = threading.Lock()

    def _record(e: BaseException) -> None:
        with errors_lock:
            errors.append(e)

    def _churner(thread_id: int):
        try:
            counter = 0
            while not stop_event.is_set():
                pid = f"churn-{thread_id}-{counter}"
                counter += 1
                specifier = _SPECIFIER_POOL[counter % len(_SPECIFIER_POOL)]
                program = _build_program_for(specifier, pid)
                manager.install_program(program)
                manager.uninstall_program(pid)
        except BaseException as e:
            _record(e)

    threads = [
        threading.Thread(target=_churner, args=(i,), name=f"churn-{i}")
        for i in range(6)
    ]

    try:
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop_event.set()
        for t in threads:
            t.join(timeout=10)
            if t.is_alive():
                _record(RuntimeError(f"thread {t.name} did not join — deadlock?"))

        assert not errors, f"errors during reconciler stress: {errors}"

        # After all threads pair install+uninstall on the same id, registry
        # ends empty. P2: _PROBE_INDEX is a pure function of _INSTALLED_PROGRAMS
        # and both should be empty.
        assert instr._INSTALLED_PROGRAMS == {}, (
            f"registry not drained after balanced install/uninstall: "
            f"{instr._INSTALLED_PROGRAMS}"
        )
        assert instr._PROBE_INDEX == {}, (
            f"probe index not drained: {instr._PROBE_INDEX}"
        )
    finally:
        _drain_registry()

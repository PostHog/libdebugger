"""
Probe-error reporting — ``$hogtrace_probe_error`` events.

When ``execute_probe`` raises (typically: the probe references a name
that doesn't resolve on the captured frame), the wrapper used to log the
exception and swallow it silently. That left developers — and the LLM
agents writing probes — with no signal beyond "I installed this probe
and never got any events". The new path emits a dedicated
``$hogtrace_probe_error`` event so the failure surfaces with enough
context to find and fix the broken probe.

Tests in this file cover:

* First failure for a (program, probe, exc-type) key fires immediately
  with ``skipped_since_last == 0``.
* Subsequent identical failures inside the dedupe window are suppressed
  and the count is accumulated.
* Window expiry re-fires with ``skipped_since_last`` reflecting the
  accumulated count.
* Distinct error types and distinct probes get distinct dedupe keys.
* ``uninstall_program`` drops dedupe state for that program so a fresh
  install starts with a clean slate.
* A broken probe does NOT block other working probes on the same
  function from firing.
* The event sink raising during error emission must not propagate.
"""

from __future__ import annotations

import threading
import time
import types

import pytest
from hogtrace.context import new_context

import libdebugger.instrumentation as instr
from test._manager_helpers import _build_program, target_mod


@pytest.fixture
def hogtrace_scope():
    with new_context():
        yield


@pytest.fixture
def sink_events(monkeypatch):
    """Install an event sink that records all (name, properties) emitted.

    Unlike the ``_enqueue_message`` monkey-patch other test files use,
    this exercises the real sink path so we catch both
    ``$hogtrace_capture`` and ``$hogtrace_probe_error`` events.
    """
    events = []

    def _sink(name, properties):
        events.append((name, properties))

    instr.set_event_sink(_sink)
    yield events
    instr.set_event_sink(None)


# ---------------------------------------------------------------------------
# First-fire behavior.
# ---------------------------------------------------------------------------


def test_broken_probe_fires_probe_error_event(hogtrace_scope, sink_events):
    """A probe that references a nonexistent name fires $hogtrace_probe_error.

    ``arg0.id`` is exactly the failure mode that prompted this feature —
    an agent wrote it expecting positional-arg access, but hogtrace
    binds args by their declared name (``x`` for ``fn_a(x=0)``). The
    VM raises ``Argument 0 not found`` and the wrapper emits a
    probe-error event.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(bad=arg0.id); }",
        program_id="prog-broken",
    )
    install_program(program)

    target_mod.fn_a(7)

    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    assert len(error_events) == 1, (
        f"expected one $hogtrace_probe_error; got {[e[0] for e in sink_events]}"
    )
    _name, props = error_events[0]
    assert props["program_id"] == "prog-broken"
    assert props["probe_id"]  # opaque id from hogtrace; just assert presence
    assert props["probe_spec"] == {
        "specifier": "test.target.fn_a",
        "target": "entry",
    }
    assert props["error_type"] == "RuntimeError"
    assert "Argument 0 not found" in props["error_message"]
    assert "Traceback" in props["traceback"]
    assert props["skipped_since_last"] == 0
    assert props["thread_id"] is not None


def test_working_exit_probe_still_fires_when_entry_probe_broken(
    hogtrace_scope, sink_events
):
    """A broken entry probe must not block other probes on the same function.

    Two probes on ``fn_a``: a broken entry and a working exit. We expect
    one $hogtrace_probe_error (from the entry) AND one $hogtrace_capture
    (from the exit). The wrapper's per-probe try/except isolates them.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(bad=arg0.id); }\n"
        "fn:test.target.fn_a:exit { capture(ok=1); }",
        program_id="prog-mixed",
    )
    install_program(program)

    target_mod.fn_a(3)

    names = [e[0] for e in sink_events]
    assert names.count("$hogtrace_probe_error") == 1, names
    assert names.count("$hogtrace_capture") == 1, names


# ---------------------------------------------------------------------------
# Dedupe — window suppression and accumulation.
# ---------------------------------------------------------------------------


def test_repeated_identical_errors_suppressed_inside_window(
    hogtrace_scope, sink_events, monkeypatch
):
    """Inside the dedupe window, identical errors fire once and are then
    suppressed. The skipped count is accumulated for the next fire.

    We set the window to a long value so all calls in this test fall
    inside it. The assertion is "exactly one event despite N failures".
    """
    from libdebugger.manager import install_program

    # Long window: every call should land inside it.
    monkeypatch.setattr(instr, "_PROBE_ERROR_WINDOW", 3600.0)

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(bad=arg0.id); }",
        program_id="prog-dedup",
    )
    install_program(program)

    for _ in range(50):
        target_mod.fn_a(0)

    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    assert len(error_events) == 1, (
        f"expected exactly 1 fire across 50 failures; got {len(error_events)}"
    )
    # The dedupe counter should reflect the 49 suppressed failures.
    key = (
        "prog-dedup",
        program.probes[0].id,
        "RuntimeError",
    )
    with instr._PROBE_ERROR_DEDUP_LOCK:
        _, suppressed = instr._PROBE_ERROR_DEDUP[key]
    assert suppressed == 49, f"expected 49 accumulated skips; got {suppressed}"


def test_window_expiry_refires_with_skipped_count(
    hogtrace_scope, sink_events, monkeypatch
):
    """After the dedupe window expires, the next failure fires again and
    carries ``skipped_since_last == <accumulated count during the window>``.

    We use a very short window (50ms) plus a real sleep so we don't have
    to monkey-patch ``time.monotonic`` (which would risk drift against
    other parts of the codebase that also call it).
    """
    from libdebugger.manager import install_program

    monkeypatch.setattr(instr, "_PROBE_ERROR_WINDOW", 0.05)

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(bad=arg0.id); }",
        program_id="prog-window",
    )
    install_program(program)

    # First fire + 4 suppressed within the window.
    target_mod.fn_a(0)  # fires
    for _ in range(4):
        target_mod.fn_a(0)  # suppressed

    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    assert len(error_events) == 1
    assert error_events[0][1]["skipped_since_last"] == 0

    # Sleep past the window, then trigger one more failure.
    time.sleep(0.1)
    target_mod.fn_a(0)

    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    assert len(error_events) == 2, (
        f"window should have expired; expected 2 fires, got {len(error_events)}"
    )
    # Second fire reports the 4 calls suppressed during the window.
    assert error_events[1][1]["skipped_since_last"] == 4


# ---------------------------------------------------------------------------
# Dedupe key — distinct error types and distinct probes.
# ---------------------------------------------------------------------------


def test_different_probes_on_same_function_dedupe_independently(
    hogtrace_scope, sink_events, monkeypatch
):
    """Two distinct broken probes on the same function get separate dedupe
    keys — each fires once even though both fail on every call.

    Without per-probe dedupe, a single fire would mask the second probe's
    failure entirely.
    """
    from libdebugger.manager import install_program

    monkeypatch.setattr(instr, "_PROBE_ERROR_WINDOW", 3600.0)

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(a=arg0.id); }\n"
        "fn:test.target.fn_a:exit { capture(b=arg99.id); }",
        program_id="prog-multi-probe",
    )
    install_program(program)

    target_mod.fn_a(0)
    target_mod.fn_a(0)

    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    # Two distinct probes -> two events on the first call. Second call
    # is suppressed for both keys.
    assert len(error_events) == 2, (
        f"expected one fire per probe; got {len(error_events)} "
        f"(targets={[e[1]['probe_spec']['target'] for e in error_events]})"
    )
    targets = {e[1]["probe_spec"]["target"] for e in error_events}
    assert targets == {"entry", "exit"}


# ---------------------------------------------------------------------------
# Uninstall clears dedupe state.
# ---------------------------------------------------------------------------


def test_uninstall_clears_dedupe_state(hogtrace_scope, sink_events, monkeypatch):
    """A program's dedupe entries must be dropped on ``uninstall_program``.

    Scenario: install a buggy program -> it fires + suppresses. Uninstall.
    Reinstall the same program id with the same buggy probe (mimics
    "agent retries with a fix that didn't actually fix it"). The next
    failure must fire immediately rather than being swallowed by the
    stale window.
    """
    from libdebugger.manager import install_program, uninstall_program

    monkeypatch.setattr(instr, "_PROBE_ERROR_WINDOW", 3600.0)

    src = "fn:test.target.fn_a:entry { capture(bad=arg0.id); }"
    install_program(_build_program(src, program_id="prog-cycle"))
    target_mod.fn_a(0)
    target_mod.fn_a(0)  # suppressed

    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    assert len(error_events) == 1

    # Uninstall must drop dedupe state for prog-cycle.
    uninstall_program("prog-cycle")
    assert not any(k[0] == "prog-cycle" for k in instr._PROBE_ERROR_DEDUP), (
        f"uninstall left stale dedupe entries: {list(instr._PROBE_ERROR_DEDUP)}"
    )

    # Reinstall and trigger again — must fire (not suppressed).
    install_program(_build_program(src, program_id="prog-cycle"))
    target_mod.fn_a(0)

    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    assert len(error_events) == 2, (
        f"reinstall after uninstall must reset dedupe; got {len(error_events)} fires"
    )
    # Fresh slate — the second fire is the first for the new install.
    assert error_events[1][1]["skipped_since_last"] == 0


# ---------------------------------------------------------------------------
# Robustness — sink raising during error emission must not propagate.
# ---------------------------------------------------------------------------


def test_sink_raising_on_error_event_does_not_propagate(hogtrace_scope):
    """If the event sink itself raises when receiving a probe-error event,
    the wrapper must swallow it — the error-reporting path can never be
    allowed to disrupt user code or the rest of the probe loop.
    """
    from libdebugger.manager import install_program

    def _bad_sink(name, props):
        raise RuntimeError("sink is on fire")

    instr.set_event_sink(_bad_sink)
    try:
        program = _build_program(
            "fn:test.target.fn_a:entry { capture(bad=arg0.id); }",
            program_id="prog-sink-fire",
        )
        install_program(program)

        # The call must succeed and return the correct value — the
        # probe-error path is fully isolated from user code.
        assert target_mod.fn_a(5) == 1 + 2 + 5
    finally:
        instr.set_event_sink(None)


# ---------------------------------------------------------------------------
# Dedupe-key correctness — distinct exception types on the same probe.
# ---------------------------------------------------------------------------


def test_distinct_exception_types_on_same_probe_dedupe_independently(monkeypatch):
    """The dedupe key is ``(program_id, probe_id, exc_type_name)`` — two
    distinct exception types for the same (program, probe) must both fire.

    In the live ``execute_probe`` path hogtrace always re-wraps probe
    failures as ``RuntimeError``, so we can't trigger two different
    Python-level exception types through it. We instead call
    ``_record_probe_error`` directly with synthetic exceptions — it's the
    function that encodes the key contract, so testing it as a unit is
    correct here.
    """
    monkeypatch.setattr(instr, "_PROBE_ERROR_WINDOW", 3600.0)

    program = types.SimpleNamespace(id="prog-mixed-exc")
    probe = types.SimpleNamespace(id="probe_0")

    # First failure of each type: both must report "fire now".
    assert instr._record_probe_error(program, probe, ValueError("x")) == 0
    assert instr._record_probe_error(program, probe, TypeError("y")) == 0

    # Second of each type within window: both suppressed.
    assert instr._record_probe_error(program, probe, ValueError("z")) is None
    assert instr._record_probe_error(program, probe, TypeError("w")) is None

    # The dedupe dict carries two independent entries — one per exc-type.
    keys_for_program = [k for k in instr._PROBE_ERROR_DEDUP if k[0] == "prog-mixed-exc"]
    assert sorted(keys_for_program) == [
        ("prog-mixed-exc", "probe_0", "TypeError"),
        ("prog-mixed-exc", "probe_0", "ValueError"),
    ], f"unexpected dedupe keys: {keys_for_program}"


# ---------------------------------------------------------------------------
# Multi-program dedupe isolation.
# ---------------------------------------------------------------------------


def test_two_programs_broken_on_same_fn_dedupe_independently(
    hogtrace_scope, sink_events, monkeypatch
):
    """Two installed programs each with a broken probe on the same function
    must each get their own dedupe entry — the first call should fire one
    event per program.

    This is the production case: multiple programs in flight, both
    happening to overlap on a hot function, both broken. A single shared
    dedupe entry would hide one program's failure behind the other.
    """
    from libdebugger.manager import install_program

    monkeypatch.setattr(instr, "_PROBE_ERROR_WINDOW", 3600.0)

    prog_a = _build_program(
        "fn:test.target.fn_a:entry { capture(bad=arg0.id); }",
        program_id="prog-multi-a",
    )
    prog_b = _build_program(
        "fn:test.target.fn_a:entry { capture(bad=arg0.id); }",
        program_id="prog-multi-b",
    )
    install_program(prog_a)
    install_program(prog_b)

    target_mod.fn_a(0)

    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    program_ids = {e[1]["program_id"] for e in error_events}
    assert program_ids == {"prog-multi-a", "prog-multi-b"}, (
        f"expected one fire per program; got program_ids={program_ids}"
    )


def test_uninstall_does_not_drop_other_programs_dedupe_state(
    hogtrace_scope, sink_events, monkeypatch
):
    """Precision check for ``_drop_dedup_for_program``: uninstalling
    program A must leave program B's dedupe entries intact.

    A bug where the helper accidentally cleared the whole dict (e.g. a
    stray ``.clear()`` instead of a per-key delete) would let B re-fire
    after the uninstall — which this test catches.
    """
    from libdebugger.manager import install_program, uninstall_program

    monkeypatch.setattr(instr, "_PROBE_ERROR_WINDOW", 3600.0)

    src = "fn:test.target.fn_a:entry { capture(bad=arg0.id); }"
    install_program(_build_program(src, program_id="prog-keep-a"))
    install_program(_build_program(src, program_id="prog-drop-b"))

    target_mod.fn_a(0)  # populates dedupe entries for both programs
    error_events = [e for e in sink_events if e[0] == "$hogtrace_probe_error"]
    assert len(error_events) == 2  # one fire per program

    # Uninstall B — only B's dedupe entries should disappear.
    uninstall_program("prog-drop-b")

    keys = list(instr._PROBE_ERROR_DEDUP.keys())
    a_keys = [k for k in keys if k[0] == "prog-keep-a"]
    b_keys = [k for k in keys if k[0] == "prog-drop-b"]
    assert a_keys, (
        f"uninstall must NOT touch prog-keep-a's dedupe state; surviving keys: {keys}"
    )
    assert not b_keys, (
        f"uninstall must drop prog-drop-b's dedupe state; surviving B keys: {b_keys}"
    )

    # Sanity: another call only re-fires for B (which has no dedupe
    # entry) — A's existing entry still suppresses.
    sink_events.clear()
    target_mod.fn_a(0)
    program_ids = {
        e[1]["program_id"] for e in sink_events if e[0] == "$hogtrace_probe_error"
    }
    # B is uninstalled now — so it can't fire either. A is still
    # installed and suppressed. So we expect zero error events.
    assert program_ids == set(), (
        f"after uninstall+call, expected no fires (B uninstalled, A "
        f"suppressed); got {program_ids}"
    )


# ---------------------------------------------------------------------------
# Concurrency — dedupe under contention.
# ---------------------------------------------------------------------------


def test_dedupe_under_concurrent_failures(monkeypatch):
    """Many threads simultaneously failing the same probe converge on a
    single fire + the correct accumulated skip count.

    The dedupe path takes ``_PROBE_ERROR_DEDUP_LOCK`` around its
    read-modify-write of the dict — a regression that dropped the lock
    (or used a non-atomic check-then-set) would let multiple threads
    each see ``prev is None`` and each return 0, producing duplicate
    fires. We assert exactly one fire and ``suppressed == N - 1``.

    Going through the full ``execute_probe`` path under threading would
    couple this test to hogtrace's request-scope thread semantics — the
    contract we care about is ``_record_probe_error``'s atomicity, so we
    call it directly from each worker.
    """
    monkeypatch.setattr(instr, "_PROBE_ERROR_WINDOW", 3600.0)

    program = types.SimpleNamespace(id="prog-concurrent")
    probe = types.SimpleNamespace(id="probe_0")

    n_threads = 16
    calls_per_thread = 100
    n_calls = n_threads * calls_per_thread
    start_barrier = threading.Barrier(n_threads)
    fires_seen: list = []
    fires_lock = threading.Lock()

    def _worker():
        start_barrier.wait(timeout=5.0)
        for _ in range(calls_per_thread):
            result = instr._record_probe_error(program, probe, ValueError("x"))
            if result is not None:
                with fires_lock:
                    fires_seen.append(result)

    threads = [
        threading.Thread(target=_worker, name=f"err-{i}") for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), f"{t.name} did not join — deadlock?"

    # Exactly one fire (the very first call to land took the "prev is None"
    # branch); all subsequent calls were inside the window and suppressed.
    assert len(fires_seen) == 1, (
        f"expected exactly one fire under concurrent failures; got "
        f"{len(fires_seen)} (skips reported: {fires_seen})"
    )
    assert fires_seen[0] == 0, (
        f"first fire must report skipped_since_last=0; got {fires_seen[0]}"
    )

    # The accumulated suppressed counter must equal n_calls - 1
    # (every call except the first one was suppressed).
    key = ("prog-concurrent", "probe_0", "ValueError")
    with instr._PROBE_ERROR_DEDUP_LOCK:
        _, suppressed = instr._PROBE_ERROR_DEDUP[key]
    assert suppressed == n_calls - 1, (
        f"expected {n_calls - 1} accumulated suppressions; got {suppressed}"
    )


# ---------------------------------------------------------------------------
# No sink registered — broken probe is silently dropped.
# ---------------------------------------------------------------------------


def test_broken_probe_with_no_sink_does_not_crash(hogtrace_scope):
    """When no event sink is registered, a broken probe must not crash
    and must not produce any side effects beyond the debug log.

    The dedupe state still gets bumped — that's harmless and the cost
    of keeping the dedupe path uniform regardless of sink wiring.
    """
    from libdebugger.manager import install_program

    # Belt and suspenders: explicitly nil the sink (the conftest reset
    # also does this, but we don't want the test depending on cleanup
    # order from a previous test).
    instr.set_event_sink(None)
    assert instr._EVENT_SINK is None

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(bad=arg0.id); }",
        program_id="prog-no-sink",
    )
    install_program(program)

    # Must not raise — and must return the correct value.
    assert target_mod.fn_a(2) == 1 + 2 + 2

    # Dedupe state was still populated (we don't gate the dedupe on the
    # presence of a sink — the sink check happens later, inside
    # _enqueue_probe_error).
    assert any(k[0] == "prog-no-sink" for k in instr._PROBE_ERROR_DEDUP), (
        "dedupe state should be populated even with no sink"
    )

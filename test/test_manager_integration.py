"""
Integration tests for ``HogTraceManager``'s top-level wiring (Phase 8).

These tests exercise the end-to-end manager surface — ``_fetch_programs``,
``start``, ``stop`` — while stubbing out the HTTP transport. We build real
``ProgramList`` payloads with hogtrace's Python constructor, serialize via
``to_bytes()``, and feed them through a mocked ``posthoganalytics.request.get``.

Kept separate from ``test_manager_property.py`` so the property suite (which
focuses on install/uninstall/update invariants) stays self-contained.
"""

from __future__ import annotations

import concurrent.futures
import logging
from types import SimpleNamespace

import pytest
from hogtrace import ProgramList
from hogtrace.vm import compile as ht_compile, package as ht_package

import libdebugger.instrumentation as instr
import libdebugger.manager as manager
from libdebugger.manager import HogTraceManager, install_program


def _build_program(source: str, program_id: str):
    """Compile a single hogtrace source snippet into a packaged ``Program``."""
    return ht_package(program_id, ht_compile(source))


def _make_program_list_bytes(programs) -> bytes:
    """Build a ``ProgramList`` from the given programs and serialize it."""
    return ProgramList(list(programs)).to_bytes()


def _make_client(personal_api_key="test-key", host="https://test.local"):
    """A minimal stand-in for ``posthoganalytics.Posthog`` with the attrs the
    manager actually reads."""
    return SimpleNamespace(personal_api_key=personal_api_key, host=host)


# ---------------------------------------------------------------------------
# _fetch_programs — happy path
# ---------------------------------------------------------------------------


def test_fetch_programs_populates_registry(monkeypatch):
    """A fetch that returns two programs populates the registry with both."""
    prog_a = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="prog_a_id",
    )
    prog_b = _build_program(
        "fn:test.target.fn_b:entry { capture(x=2); }",
        program_id="prog_b_id",
    )
    payload = _make_program_list_bytes([prog_a, prog_b])

    def fake_get(api_key, url, host, timeout=None):
        assert api_key == "test-key"
        assert host == "https://test.local"
        assert "live_debugger/programs/active" in url
        return SimpleNamespace(content=payload)

    monkeypatch.setattr(manager, "get", fake_get)

    mgr = HogTraceManager(_make_client())
    mgr._fetch_programs()

    assert set(instr._INSTALLED_PROGRAMS.keys()) == {"prog_a_id", "prog_b_id"}


# ---------------------------------------------------------------------------
# _fetch_programs — error handling
# ---------------------------------------------------------------------------


def test_fetch_programs_survives_http_error(monkeypatch, caplog):
    """A network exception during fetch is logged and swallowed; the registry
    stays exactly as it was before the call.
    """

    def fake_get(*args, **kwargs):
        raise Exception("network down")

    monkeypatch.setattr(manager, "get", fake_get)

    caplog.set_level(logging.ERROR, logger="libdebugger.manager")

    snapshot_before = dict(instr._INSTALLED_PROGRAMS)
    mgr = HogTraceManager(_make_client())
    mgr._fetch_programs()  # MUST NOT raise

    assert dict(instr._INSTALLED_PROGRAMS) == snapshot_before


def test_fetch_programs_survives_bad_payload(monkeypatch, caplog):
    """A malformed ``ProgramList`` payload is logged and swallowed; the registry
    stays exactly as it was before the call.
    """
    monkeypatch.setattr(
        manager,
        "get",
        lambda *a, **k: SimpleNamespace(content=b"\x00\x01\x02junk"),
    )

    caplog.set_level(logging.ERROR, logger="libdebugger.manager")

    snapshot_before = dict(instr._INSTALLED_PROGRAMS)
    mgr = HogTraceManager(_make_client())
    mgr._fetch_programs()  # MUST NOT raise

    assert dict(instr._INSTALLED_PROGRAMS) == snapshot_before


# ---------------------------------------------------------------------------
# _fetch_programs — reconcile (install / uninstall / update)
# ---------------------------------------------------------------------------


def test_fetch_programs_reconciles_uninstalls(monkeypatch):
    """A program that was previously installed but is absent from the next
    fetch must be removed from the registry."""
    pre = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="pre-installed",
    )
    install_program(pre)
    assert "pre-installed" in instr._INSTALLED_PROGRAMS

    # Next fetch returns a different program; the pre-installed one disappears.
    new_prog = _build_program(
        "fn:test.target.fn_b:entry { capture(x=2); }",
        program_id="new-prog",
    )
    payload = _make_program_list_bytes([new_prog])
    monkeypatch.setattr(
        manager, "get", lambda *a, **k: SimpleNamespace(content=payload)
    )

    mgr = HogTraceManager(_make_client())
    mgr._fetch_programs()

    assert "pre-installed" not in instr._INSTALLED_PROGRAMS
    assert "new-prog" in instr._INSTALLED_PROGRAMS


def test_fetch_programs_reconciles_hash_change(monkeypatch):
    """Two programs sharing an id but with different ``hash`` should trigger
    an update_program call so the registry holds the new version.

    Currently hogtrace.package() hardcodes hash="test" for every packaged
    program, so we cannot actually exercise the hash-change branch end-to-end
    until that lands upstream. We assert the limitation and skip with a TODO.
    """
    prog_a = _build_program(
        "fn:test.target.fn_a:entry { capture(x=1); }",
        program_id="shared-id",
    )
    prog_b = _build_program(
        "fn:test.target.fn_b:entry { capture(x=2); }",
        program_id="shared-id",
    )
    if prog_a.hash == prog_b.hash:
        pytest.skip(
            "hogtrace.package hardcodes hash='test'; cannot test hash-change "
            "reconcile yet. TODO: re-enable once hogtrace computes a real hash."
        )

    install_program(prog_a)
    assert instr._INSTALLED_PROGRAMS["shared-id"] is prog_a

    payload = _make_program_list_bytes([prog_b])
    monkeypatch.setattr(
        manager, "get", lambda *a, **k: SimpleNamespace(content=payload)
    )

    mgr = HogTraceManager(_make_client())
    mgr._fetch_programs()

    assert instr._INSTALLED_PROGRAMS["shared-id"] is prog_b


# ---------------------------------------------------------------------------
# stop() — must not deadlock
# ---------------------------------------------------------------------------


def test_stop_uninstalls_all_no_deadlock():
    """``stop()`` must release ``_LOCK`` before iterating per-program uninstalls.

    The spec's original sketch held ``_LOCK`` across the iteration; since
    ``_LOCK`` is a non-reentrant ``threading.Lock`` and ``uninstall_program``
    re-acquires it, that would deadlock on the very first uninstall. We run
    ``stop()`` inside a worker thread with a hard 5-second timeout — if the
    method deadlocks, ``future.result(timeout=5)`` raises
    ``concurrent.futures.TimeoutError`` and the test fails loudly.
    """
    for i, fn_name in enumerate(("fn_a", "fn_b", "fn_c")):
        prog = _build_program(
            f"fn:test.target.{fn_name}:entry {{ capture(x={i}); }}",
            program_id=f"stop-prog-{i}",
        )
        install_program(prog)

    assert len(instr._INSTALLED_PROGRAMS) == 3

    # Use a no-API-key client so start() never spawns a poller — we are only
    # testing the stop() path here.
    mgr = HogTraceManager(_make_client(personal_api_key=None))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(mgr.stop)
        # If stop() deadlocks, this raises concurrent.futures.TimeoutError.
        future.result(timeout=5)

    assert instr._INSTALLED_PROGRAMS == {}


# ---------------------------------------------------------------------------
# start() — no-API-key short-circuit
# ---------------------------------------------------------------------------


def test_start_with_no_api_key_does_not_poll():
    """Without a personal_api_key, ``start()`` must NOT create a poller.

    Production servers without configured keys should still allow the SDK to
    be present in code without spinning a background thread that fails on
    every tick. We assert the absence of a poller rather than the absence of
    a warning log — easier to verify and matches the spec.
    """
    mgr = HogTraceManager(_make_client(personal_api_key=None))
    mgr.start()
    assert mgr.poller is None
    # And calling stop() on a never-started manager is a no-op.
    mgr.stop()

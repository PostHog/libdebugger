"""
Regression test for the example Flask app's status snapshot.

Codex review found ``/_libdebugger/status`` still walked ``vars(services)``
for ``hasattr(obj, "__posthog_decorator")`` — a marker that no longer
exists on the production path — and silently reported an empty list of
instrumented functions even when local probes were installed.

The fix is to use ``libdebugger.instrumentation.is_instrumented`` instead.
This test exercises the snapshot helper directly so we don't pull
Flask / python-dotenv into the libdebugger test dependencies just to
cover an example app.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "example"


@pytest.fixture
def example_modules(monkeypatch):
    """Import the example's ``services`` / ``probes`` / ``status`` modules
    fresh inside the test, then tear them down. Avoids both bleeding state
    between tests and contaminating the importing test process.
    """
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    for name in ("services", "probes", "status"):
        sys.modules.pop(name, None)

    import services as example_services  # noqa: PLC0415
    import probes as example_probes  # noqa: PLC0415
    import status as example_status  # noqa: PLC0415

    yield example_services, example_probes, example_status

    for name in ("services", "probes", "status"):
        sys.modules.pop(name, None)


def test_status_snapshot_reports_instrumented_services(example_modules):
    """Once local probes are installed, the status snapshot must name the
    instrumented services functions — not return an empty list as the
    previous ``hasattr(__posthog_decorator)`` path did.
    """
    _example_services, example_probes, example_status = example_modules

    installed = example_probes.install_local_probes()
    assert installed, "test invariant: local probes must install successfully"

    snapshot = example_status.build_status_snapshot()

    assert any(pid.startswith("local-") for pid in snapshot["installed_programs"]), (
        f"expected at least one local-N program; got {snapshot['installed_programs']}"
    )

    expected_any = {"get_user", "create_order", "slow_compute"}
    reported = set(snapshot.get("instrumented_functions", []))
    assert expected_any & reported, (
        f"snapshot reported zero instrumented service functions; "
        f"expected at least one of {expected_any}, got {reported}"
    )

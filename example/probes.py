"""Local probe definitions for the Flask example.

In production, ``HogTraceManager`` polls PostHog's control plane for a
``ProgramList`` and installs each ``Program``. For local iteration we
short-circuit that by compiling a small set of probes here and calling
``install_program`` directly on app startup. Edit ``LOCAL_PROBE_SOURCES``
below to add new probes, then bounce the server.
"""

from __future__ import annotations

import logging
from typing import List

import hogtrace
from libdebugger import manager


logger = logging.getLogger(__name__)


# Each entry compiles into one Program. Keep the bodies small — the probe
# DSL is HogTrace's; see PostHog/hogtrace for what's available. ``capture(...)``
# emits the named values as event properties when the probe fires.
LOCAL_PROBE_SOURCES: List[str] = [
    # Trace user lookups. Entry probes run inside the function's own
    # frame so we reference parameter names directly (e.g. `user_id`),
    # not positional indices.
    """
    fn:services.get_user:entry {
        capture(user_id=user_id);
    }
    fn:services.get_user:exit {
        capture(user_id=user_id);
    }
    """,
    # Watch order creation. Captures the request shape on entry.
    """
    fn:services.create_order:entry {
        capture(user_id=user_id, item=item, qty=qty);
    }
    """,
    # Time the slow path.
    """
    fn:services.slow_compute:entry {
        capture(n=n);
    }
    fn:services.slow_compute:exit {
        capture(n=n);
    }
    """,
]


def install_local_probes() -> List[str]:
    """Compile and install every program in ``LOCAL_PROBE_SOURCES``.

    Returns the list of installed program IDs.
    """
    installed_ids: List[str] = []
    for i, source in enumerate(LOCAL_PROBE_SOURCES):
        program_id = f"local-{i}"
        try:
            bytecode = hogtrace.compile(source)
            program = hogtrace.package(program_id, bytecode)
            manager.install_program(program)
            installed_ids.append(program_id)
            logger.info(
                "installed local program %s with %d probe(s)",
                program_id,
                len(program.probes),
            )
        except Exception:
            logger.exception("failed to install local program %s", program_id)
    return installed_ids


def uninstall_local_probes(program_ids: List[str]) -> None:
    """Tear down every program installed by :func:`install_local_probes`."""
    for pid in program_ids:
        try:
            manager.uninstall_program(pid)
        except Exception:
            logger.exception("failed to uninstall %s", pid)

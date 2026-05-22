"""Build the introspection snapshot the ``/_libdebugger/status`` route returns.

Pulled out of ``app.py`` so the snapshot logic can be exercised without
having to spin up the full Flask app (which pulls Flask + python-dotenv
into the test environment). The route in ``app.py`` is a thin wrapper.
"""

from __future__ import annotations

from typing import Any, Dict, List

from libdebugger import instrumentation as _instr

import services


def build_status_snapshot() -> Dict[str, Any]:
    """Snapshot of what libdebugger has installed right now."""
    instrumented_functions: List[str] = [
        name
        for name, obj in vars(services).items()
        if callable(obj) and _instr.is_instrumented(obj)
    ]
    return {
        "installed_programs": list(_instr._INSTALLED_PROGRAMS.keys()),
        "probe_index": {
            f"{qualname}:{kind}": [(p.id, pr.id) for p, pr in pairs]
            for (qualname, kind), pairs in _instr._PROBE_INDEX.items()
        },
        "instrumented_functions": instrumented_functions,
        "event_sink": "configured" if _instr._EVENT_SINK is not None else "none",
    }

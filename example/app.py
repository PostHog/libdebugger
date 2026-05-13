"""Tiny Flask app wired up to libdebugger end-to-end.

What you get when this starts:

* The functions in ``services.py`` are valid probe targets — their
  qualnames line up with ``example.services.<name>``.
* ``probes.py`` compiles a small set of probes and installs them at
  startup so probe firing is observable without a PostHog server.
* Every request runs inside a hogtrace request scope so probes have a
  ``get_store()`` they can write to.
* If ``POSTHOG_PROJECT_API_KEY`` is set, captures flow to a real
  PostHog instance via the ``posthoganalytics`` SDK. Otherwise, captures
  are printed to stdout so you can see probes fire while iterating.

Env vars (all optional):

* ``POSTHOG_PROJECT_API_KEY``  — phc_... project key for event capture.
* ``POSTHOG_PERSONAL_API_KEY`` — phx_... personal key for control-plane
                                 polling. If unset, the manager doesn't
                                 spawn its poller (local-probe mode).
* ``POSTHOG_HOST``             — defaults to https://us.i.posthog.com.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

# Load .env BEFORE reading any POSTHOG_* env vars below. Looks for a
# .env file next to this app.py so the path is stable regardless of CWD.
# Existing process env wins (override=False) — a shell-level export
# still beats whatever's in the file.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from flask import Flask, g, jsonify, request  # noqa: E402

import libdebugger  # noqa: E402
from libdebugger import instrumentation as _instr  # noqa: E402
from libdebugger.manager import HogTraceManager  # noqa: E402
from hogtrace.context import new_context  # noqa: E402

import probes  # noqa: E402
import services  # noqa: E402


logging.basicConfig(
    level=os.environ.get("LIBDEBUGGER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("example.app")


# ---------------------------------------------------------------------------
# PostHog client + manager setup.
# ---------------------------------------------------------------------------

POSTHOG_PROJECT_KEY = os.environ.get("POSTHOG_PROJECT_API_KEY")
POSTHOG_PERSONAL_KEY = os.environ.get("POSTHOG_PERSONAL_API_KEY")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")


def _stdout_sink(event_name: str, properties: Dict[str, Any]) -> None:
    """Pretty-print captures to stdout. Useful when no PostHog is wired."""
    payload = {k: v for k, v in properties.items() if k != "captures"}
    captures = properties.get("captures")
    print(
        f"[probe] {event_name} "
        f"program={payload['program_id']} probe={payload['probe_id']} "
        f"spec={payload['probe_spec']} "
        f"captures={json.dumps(captures, default=str)}",
        file=sys.stderr,
        flush=True,
    )


_manager: HogTraceManager | None = None
_installed_program_ids: list[str] = []


def _setup_libdebugger() -> None:
    """Initialize the manager and install local probes."""
    global _manager, _installed_program_ids

    if POSTHOG_PROJECT_KEY:
        # Real PostHog: build a Posthog client and hand it to the manager.
        # The manager wires client.capture as the event sink automatically.
        from posthoganalytics import Posthog

        client = Posthog(
            project_api_key=POSTHOG_PROJECT_KEY,
            host=POSTHOG_HOST,
            personal_api_key=POSTHOG_PERSONAL_KEY,
        )
        _manager = HogTraceManager(client, poll_interval=30)
        _manager.start()
        logger.info(
            "HogTraceManager started (host=%s, polling=%s)",
            POSTHOG_HOST,
            bool(POSTHOG_PERSONAL_KEY),
        )
    else:
        # Local-only mode: no PostHog. Route captures to stdout so the
        # demo is observable without any external service.
        libdebugger.set_event_sink(_stdout_sink)
        logger.info(
            "No POSTHOG_PROJECT_API_KEY set; running in local-only mode "
            "(captures printed to stderr).",
        )

    # Install the hand-written probes from probes.py. In production these
    # would come from the control plane via the manager's poller — locally
    # we just bypass that and call install_program directly.
    _installed_program_ids = probes.install_local_probes()
    logger.info("Installed %d local program(s)", len(_installed_program_ids))


# ---------------------------------------------------------------------------
# Flask app.
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.before_request
def _start_hogtrace_scope() -> None:
    """Open a hogtrace request scope so probes have somewhere to store state."""
    ctx = new_context()
    ctx.__enter__()
    g._hogtrace_ctx = ctx


@app.teardown_request
def _end_hogtrace_scope(_exc: BaseException | None) -> None:
    """Close the per-request hogtrace scope. Idempotent."""
    ctx = g.pop("_hogtrace_ctx", None)
    if ctx is not None:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            logger.exception("teardown of hogtrace scope failed")


# ---------------------------------------------------------------------------
# Domain routes.
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/users/<int:user_id>")
def get_user(user_id: int):
    user = services.get_user(user_id)
    if user is None:
        return {"error": "user not found", "user_id": user_id}, 404
    return user


@app.post("/users")
def create_user():
    body = request.get_json(silent=True) or {}
    try:
        user = services.create_user(body.get("name", ""), body.get("email", ""))
    except ValueError as e:
        return {"error": str(e)}, 400
    return user, 201


@app.get("/users/<int:user_id>/orders")
def list_orders(user_id: int):
    return jsonify(services.list_orders_for_user(user_id))


@app.post("/orders")
def create_order():
    body = request.get_json(silent=True) or {}
    try:
        order = services.create_order(
            user_id=int(body.get("user_id", 0)),
            item=body.get("item", ""),
            qty=int(body.get("qty", 0)),
        )
    except LookupError as e:
        return {"error": str(e)}, 404
    except ValueError as e:
        return {"error": str(e)}, 400
    return order, 201


@app.get("/slow/<int:n>")
def slow(n: int):
    return {"n": n, "result": services.slow_compute(n)}


# ---------------------------------------------------------------------------
# Introspection.
# ---------------------------------------------------------------------------


@app.get("/_libdebugger/status")
def libdebugger_status():
    """Snapshot of what's installed in libdebugger's registry."""
    return {
        "installed_programs": list(_instr._INSTALLED_PROGRAMS.keys()),
        "probe_index": {
            f"{qualname}:{kind}": [(p.id, pr.id) for p, pr in pairs]
            for (qualname, kind), pairs in _instr._PROBE_INDEX.items()
        },
        "wrapped_functions": [
            name
            for name, obj in vars(services).items()
            if hasattr(obj, "__posthog_decorator")
        ],
        "event_sink": "configured" if _instr._EVENT_SINK is not None else "none",
        "manager_running": _manager is not None and _manager.enabled,
    }


# ---------------------------------------------------------------------------
# Bootstrap.
# ---------------------------------------------------------------------------

# Initialize libdebugger BEFORE the first request so probes are installed
# at startup. With Flask's reloader, this runs once per process; the
# child reloader process also gets its own initialization.
_setup_libdebugger()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

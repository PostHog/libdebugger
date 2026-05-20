"""
Shared pytest fixtures.

The ``reset_state`` autouse fixture returns the world to a clean slate
between tests:

* Module-level dispatch state on ``libdebugger.instrumentation`` is
  cleared (``_PROBE_INDEX``, ``_INSTALLED_PROGRAMS``, ``_CODE_PROBE_INDEX``,
  ``_FUNCTIONS_BY_CODE``).
* Any ``__posthog_decorator`` sentinel lingering on a target function is
  removed.
* ``sys.monitoring`` events are disabled AND — uniquely for tests — the
  tool slot is explicitly freed. Production code holds the slot for the
  process lifetime; tests need a clean slate so the tool-slot tests can
  exercise the full acquisition path each time.
* If a test monkeypatched ``_enqueue_message`` without ``monkeypatch``,
  the original is restored.
"""

from __future__ import annotations

import sys

import pytest

import libdebugger.instrumentation as instr
from test import target as target_module

POSTHOG_DECORATOR_ATTR = "__posthog_decorator"


@pytest.fixture(autouse=True)
def reset_state():
    original_enqueue = getattr(instr, "_enqueue_message", None)

    yield

    # Disable events for our tool slot (if any).
    try:
        instr._release_tool()
    except Exception:
        pass

    # Then forcibly free the slot too — only the test harness does this;
    # production keeps the slot held for the lifetime of the process.
    if instr._TOOL_ID != -1:
        try:
            sys.monitoring.free_tool_id(instr._TOOL_ID)
        except Exception:
            pass
    instr._TOOL_ID = -1
    instr._CALLBACKS_REGISTERED = False

    instr._PROBE_INDEX = {}
    instr._INSTALLED_PROGRAMS = {}
    instr._CODE_PROBE_INDEX = {}
    instr._FUNCTIONS_BY_CODE = {}
    instr._MONITORED_CODES.clear()

    instr._EVENT_SINK = None

    for _name, obj in list(vars(target_module).items()):
        if hasattr(obj, POSTHOG_DECORATOR_ATTR):
            try:
                delattr(obj, POSTHOG_DECORATOR_ATTR)
            except AttributeError:
                pass

        if isinstance(obj, type):
            for _mname, mobj in list(vars(obj).items()):
                if hasattr(mobj, POSTHOG_DECORATOR_ATTR):
                    try:
                        delattr(mobj, POSTHOG_DECORATOR_ATTR)
                    except AttributeError:
                        pass

    if (
        original_enqueue is not None
        and getattr(instr, "_enqueue_message", None) is not original_enqueue
    ):
        instr._enqueue_message = original_enqueue

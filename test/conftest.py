"""
Shared pytest fixtures.

The ``reset_state`` autouse fixture returns the world to a clean slate
between tests:

* Module-level dispatch state on ``libdebugger.instrumentation`` is
  cleared (``_PROBE_INDEX``, ``_INSTALLED_PROGRAMS``, ``_CODE_TO_QUALNAME``).
* Any ``__posthog_decorator`` sentinel lingering on a target function is
  removed.
* ``sys.monitoring`` is fully released so each test gets a fresh tool-id
  registration when it calls ``install_program``.
* If a test monkeypatched ``_enqueue_message`` without ``monkeypatch``,
  the original is restored.
"""

from __future__ import annotations

import pytest

import libdebugger.instrumentation as instr
from test import target as target_module

POSTHOG_DECORATOR_ATTR = "__posthog_decorator"


@pytest.fixture(autouse=True)
def reset_state():
    original_enqueue = getattr(instr, "_enqueue_message", None)

    yield

    # Release the sys.monitoring tool id and disable all events. This is
    # the load-bearing teardown — without it a code object monitored by
    # test N keeps firing dispatch callbacks during test N+1.
    try:
        instr._release_tool()
    except Exception:
        pass

    instr._PROBE_INDEX = {}
    instr._INSTALLED_PROGRAMS = {}
    instr._CODE_TO_QUALNAME = {}
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

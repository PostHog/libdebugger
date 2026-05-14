"""
Shared pytest fixtures for the hogtrace-manager property tests.

The ``reset_state`` autouse fixture is responsible for returning the world to
a clean slate after every test:

* Module-level registries on ``libdebugger.instrumentation`` are cleared.
  Those globals (``_PROBE_INDEX`` and ``_INSTALLED_PROGRAMS``) don't exist
  yet - they get introduced in Phase 2. The fixture must therefore tolerate
  their absence on the current ``main``/``feat/hogtrace-manager`` HEAD.

* Any ``InstrumentationDecorator`` lingering on a target function gets
  unwrapped by calling its ``cleanup()`` and detaching the
  ``__posthog_decorator`` attribute. Without this, a test that crashes mid-
  instrument would leave bytecode mutations bleeding into the next test.

* If a test monkeypatched ``libdebugger.instrumentation._enqueue_message``,
  the original is restored.
"""

from __future__ import annotations

import pytest

import libdebugger.instrumentation as instr
from test import target as target_module

# Attribute name attached to instrumented callables. Locks in the convention
# that production code (Phase 2+) will need to match.
POSTHOG_DECORATOR_ATTR = "__posthog_decorator"


@pytest.fixture(autouse=True)
def reset_state():
    """Run after every test to wipe instrumentation side effects."""
    # Snapshot the original _enqueue_message so we can restore it even if a
    # test patched it without using monkeypatch.
    original_enqueue = getattr(instr, "_enqueue_message", None)

    yield

    # 1. Clear registries. These attributes are introduced in Phase 2; on
    #    earlier commits they simply don't exist and we leave the module
    #    alone for those attrs.
    for attr in ("_PROBE_INDEX", "_INSTALLED_PROGRAMS"):
        if hasattr(instr, attr):
            setattr(instr, attr, {})

    # 1b. Reset the global event sink so a sink registered by one test
    #     can't leak captures into the next test's _enqueue_message path.
    if hasattr(instr, "_EVENT_SINK"):
        instr._EVENT_SINK = None

    # 2. Walk the target module and tear down any lingering decorators. We
    #    iterate over a snapshot of ``vars()`` because cleanup may mutate the
    #    namespace (deleting POSTHOG_DECORATOR_ATTR from a function).
    for name, obj in list(vars(target_module).items()):
        # Plain functions.
        if hasattr(obj, POSTHOG_DECORATOR_ATTR):
            dec = getattr(obj, POSTHOG_DECORATOR_ATTR)
            try:
                dec.cleanup()
            except Exception:
                pass
            try:
                delattr(obj, POSTHOG_DECORATOR_ATTR)
            except AttributeError:
                pass

        # Methods on classes - walk class dicts one level deep.
        if isinstance(obj, type):
            for _mname, mobj in list(vars(obj).items()):
                if hasattr(mobj, POSTHOG_DECORATOR_ATTR):
                    dec = getattr(mobj, POSTHOG_DECORATOR_ATTR)
                    try:
                        dec.cleanup()
                    except Exception:
                        pass
                    try:
                        delattr(mobj, POSTHOG_DECORATOR_ATTR)
                    except AttributeError:
                        pass

    # 3. Restore _enqueue_message in case a test patched it without using
    #    monkeypatch.
    if (
        original_enqueue is not None
        and getattr(instr, "_enqueue_message", None) is not original_enqueue
    ):
        instr._enqueue_message = original_enqueue

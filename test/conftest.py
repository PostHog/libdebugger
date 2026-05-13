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

    # 2. Walk the target module and tear down any lingering decorators. We
    #    iterate over a snapshot of ``vars()`` because cleanup may mutate the
    #    namespace (deleting ``__posthog_decorator`` from a function).
    for name, obj in list(vars(target_module).items()):
        # Plain functions.
        if hasattr(obj, "__posthog_decorator"):
            dec = obj.__posthog_decorator
            try:
                dec.cleanup()
            except Exception:
                pass
            try:
                del obj.__posthog_decorator
            except AttributeError:
                pass

        # Methods on classes - walk class dicts one level deep.
        if isinstance(obj, type):
            for _mname, mobj in list(vars(obj).items()):
                if hasattr(mobj, "__posthog_decorator"):
                    dec = mobj.__posthog_decorator
                    try:
                        dec.cleanup()
                    except Exception:
                        pass
                    try:
                        del mobj.__posthog_decorator
                    except AttributeError:
                        pass

    # 3. Restore _enqueue_message in case a test patched it without using
    #    monkeypatch.
    if (
        original_enqueue is not None
        and getattr(instr, "_enqueue_message", None) is not original_enqueue
    ):
        instr._enqueue_message = original_enqueue

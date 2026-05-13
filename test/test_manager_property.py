"""
Property tests for the hogtrace-manager rewrite.

Phase 0 leaves this file as a placeholder so subsequent phases (P1..P8) can
fill it in. The only job here is to confirm that the production modules and
the test strategies module import cleanly from a pytest collection pass.
"""

# Production-side imports. If the manager / instrumentation modules can't be
# imported, pytest collection fails fast and we know about it.
import libdebugger.instrumentation  # noqa: F401
import libdebugger.manager  # noqa: F401

# Test-side helpers. Same rationale: a missing strategy or syntax error in
# strategies.py should surface as a collection error, not a runtime one.
from test import strategies  # noqa: F401
from test import target  # noqa: F401

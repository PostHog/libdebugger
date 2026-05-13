"""
Property tests for the hogtrace-manager rewrite.

Phase 0 leaves this file as a placeholder so subsequent phases (P1..P8) can
fill it in. The only job here is to confirm that the production modules and
the test strategies module import cleanly from a pytest collection pass.
"""


def test_imports_clean():
    """Sanity check that the production modules import without error.

    Phase 1+ tests live in this file; for now, this is a placeholder that
    fails loudly if the import surface breaks.
    """
    import libdebugger.instrumentation  # noqa: F401
    import libdebugger.manager  # noqa: F401
    from test import strategies, target  # noqa: F401

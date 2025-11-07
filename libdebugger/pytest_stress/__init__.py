"""
Pytest plugin for stress-testing libdebugger instrumentation.

This plugin randomly instruments functions during test execution to ensure that
the instrumentation system doesn't break application code.
"""

from .plugin import (
    pytest_addoption,
    pytest_configure,
    pytest_collection_modifyitems,
    pytest_runtest_setup,
    pytest_runtest_makereport,
    pytest_runtest_call,
    pytest_runtest_teardown,
    pytest_sessionfinish,
)

__all__ = [
    "pytest_addoption",
    "pytest_configure",
    "pytest_collection_modifyitems",
    "pytest_runtest_setup",
    "pytest_runtest_makereport",
    "pytest_runtest_call",
    "pytest_runtest_teardown",
    "pytest_sessionfinish",
]

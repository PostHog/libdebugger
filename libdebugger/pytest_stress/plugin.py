"""
Main pytest plugin implementation for instrumentation stress testing.

This module implements pytest hooks to randomly instrument functions during test
execution and detect failures caused by instrumentation.
"""

import random
from typing import Optional, List

import pytest

from libdebugger.instrumentation import InstrumentationDecorator
from hogtrace.vm import compile as hogtrace_compile, package
from hogtrace.context import new_context

from .discovery import discover_all_functions, get_function_info
from .tracker import InstrumentationTracker
from .reporter import FailureReporter


# Global state for the plugin
_tracker: Optional[InstrumentationTracker] = None
_reporter: Optional[FailureReporter] = None
_config: dict = {}
_test_context_manager = None
_functions_pool: List = []
_test_counter = 0
_rerun_state: dict = {}  # test_id -> rerun_count


def pytest_addoption(parser):
    """Add plugin configuration options."""
    group = parser.getgroup("libdebugger_stress")
    group.addoption(
        "--libdebugger-stress",
        action="store_true",
        default=False,
        help="Enable libdebugger instrumentation stress testing",
    )
    group.addoption(
        "--stress-num-functions",
        action="store",
        type=int,
        default=50,
        help="Number of functions to instrument (default: 50)",
    )
    group.addoption(
        "--stress-rotation-interval",
        action="store",
        type=int,
        default=10,
        help="Number of tests between instrumentation rotation (default: 10)",
    )
    group.addoption(
        "--stress-max-reruns",
        action="store",
        type=int,
        default=2,
        help="Maximum number of test reruns without instrumentation (default: 2)",
    )

    # Register ini options
    parser.addini(
        "libdebugger_stress_enabled",
        "Enable instrumentation stress testing",
        type="bool",
        default=False,
    )
    parser.addini(
        "libdebugger_stress_num_functions",
        "Number of functions to instrument",
        type="string",
        default="50",
    )
    parser.addini(
        "libdebugger_stress_rotation_interval",
        "Number of tests between instrumentation rotation",
        type="string",
        default="10",
    )
    parser.addini(
        "libdebugger_stress_max_reruns",
        "Maximum number of test reruns",
        type="string",
        default="2",
    )


def pytest_configure(config):
    """Initialize the plugin."""
    global _tracker, _reporter, _config

    # Check if plugin is enabled
    enabled = config.getoption("--libdebugger-stress", False)

    # Also check pytest.ini configuration
    if not enabled:
        enabled = config.getini("libdebugger_stress_enabled")
        if isinstance(enabled, str):
            enabled = enabled.lower() in ("true", "1", "yes")

    if not enabled:
        return

    # Initialize global state
    _tracker = InstrumentationTracker()
    _reporter = FailureReporter()

    # Store configuration
    _config["enabled"] = True
    _config["num_functions"] = config.getoption("--stress-num-functions", 50)
    _config["rotation_interval"] = config.getoption("--stress-rotation-interval", 10)
    _config["max_reruns"] = config.getoption("--stress-max-reruns", 2)

    # Check ini options as well
    if config.getini("libdebugger_stress_num_functions"):
        _config["num_functions"] = int(
            config.getini("libdebugger_stress_num_functions")
        )
    if config.getini("libdebugger_stress_rotation_interval"):
        _config["rotation_interval"] = int(
            config.getini("libdebugger_stress_rotation_interval")
        )
    if config.getini("libdebugger_stress_max_reruns"):
        _config["max_reruns"] = int(config.getini("libdebugger_stress_max_reruns"))

    # Set terminal writer for reporter
    if hasattr(config, "_terminal_writer"):
        _reporter.set_terminal_writer(config._terminal_writer)

    # Print configuration
    terminal_reporter = config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter:
        terminal_reporter.write_line(
            f"\n[libdebugger-stress] Stress testing enabled: "
            f"{_config['num_functions']} functions, "
            f"rotation every {_config['rotation_interval']} tests, "
            f"max {_config['max_reruns']} reruns\n",
            bold=True,
            cyan=True,
        )


def pytest_collection_modifyitems(session, config, items):
    """Hook called after test collection (currently unused)."""
    # We don't discover functions here anymore - we do it lazily in pytest_runtest_setup
    # when modules are actually loaded
    pass


def _create_tracking_probes():
    """Create probes that capture execution information."""
    probe_source = """
        fn:*:entry {
            capture(executed=true, args=args, kwargs=kwargs);
        }
        fn:*:exit {
            capture(returned=true, result=result);
        }
    """
    program = hogtrace_compile(probe_source)
    pkg = package("pytest-stress", program)

    return pkg, program


def _instrument_random_functions(terminal_reporter=None):
    """Instrument a random selection of functions."""
    global _tracker, _functions_pool

    if not _functions_pool:
        return

    # Determine how many functions to instrument
    num_to_instrument = min(_config["num_functions"], len(_functions_pool))

    # Randomly select functions
    selected_functions = random.sample(_functions_pool, num_to_instrument)

    # Create probes
    pkg, program = _create_tracking_probes()
    entry_probe = (pkg, program.probes[0])
    exit_probe = (pkg, program.probes[1])

    probe_source = """
fn:*:entry {
    capture(executed=true, args=args, kwargs=kwargs);
}
fn:*:exit {
    capture(returned=true, result=result);
}
"""

    # Instrument each function
    instrumented_count = 0
    instrumented_functions = []
    for func in selected_functions:
        try:
            function_info = get_function_info(func)

            decorator = InstrumentationDecorator(func, {entry_probe}, {exit_probe})

            _tracker.add_instrumentation(
                func, decorator, function_info, probe_source=probe_source
            )
            instrumented_count += 1
            instrumented_functions.append(function_info)
        except Exception as e:
            # Skip functions that can't be instrumented
            if terminal_reporter:
                terminal_reporter.write_line(
                    f"[libdebugger-stress] Warning: Failed to instrument {func.__name__}: {e}",
                    yellow=True,
                )
            continue

    if instrumented_count > 0 and terminal_reporter:
        terminal_reporter.write_line(
            f"\n[libdebugger-stress] Instrumented {instrumented_count}/{len(_functions_pool)} functions:",
            cyan=True,
            bold=True,
        )
        for func_info in instrumented_functions:
            # Format with colors: function name in cyan/green, path in white/grey
            func_name = f"{func_info['module']}.{func_info['qualname']}"
            if func_info["file"] and func_info["line"]:
                # Use ANSI codes for inline colors: cyan for function, white for path
                location = f"  - \033[96m{func_name}\033[0m \033[37m({func_info['file']}:{func_info['line']})\033[0m"
            else:
                location = f"  - \033[96m{func_name}\033[0m"
            terminal_reporter.write_line(location)


def _cleanup_instrumentation():
    """Clean up all current instrumentation."""
    global _tracker

    if _tracker:
        _tracker.cleanup_all()


def _rotate_instrumentation(terminal_reporter=None):
    """Rotate instrumentation by removing some and adding new ones."""
    global _tracker, _functions_pool

    if terminal_reporter:
        terminal_reporter.write_line(
            f"\n[libdebugger-stress] Rotating instrumentation (test #{_test_counter})",
            yellow=True,
            bold=True,
        )

    # Clean up current instrumentation
    _cleanup_instrumentation()

    # Re-instrument with new random selection
    _instrument_random_functions(terminal_reporter)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Set up instrumentation before each test."""
    global _tracker, _test_counter, _test_context_manager, _functions_pool

    if not _config.get("enabled"):
        return

    # Lazy discovery on first test - force import of project modules
    if not _functions_pool:
        # Force import of project modules before discovery
        # This ensures the modules are in sys.modules for discovery to find them
        try:
            import importlib
            import pkgutil
            from .discovery import get_project_root, get_project_name

            # Get the project name dynamically
            project_root = get_project_root()
            if project_root:
                project_name = get_project_name(project_root)
                if project_name:
                    try:
                        # Import the main project module
                        project_module = importlib.import_module(project_name)

                        # Auto-discover and import all project submodules
                        for _, modname, _ in pkgutil.walk_packages(
                            path=project_module.__path__,
                            prefix=project_module.__name__ + ".",
                            onerror=lambda _: None,
                        ):
                            # Skip the pytest_stress module itself
                            if "pytest_stress" in modname:
                                continue
                            try:
                                importlib.import_module(modname)
                            except Exception:
                                # Skip modules that can't be imported
                                pass
                    except Exception:
                        pass
        except Exception:
            pass

        _functions_pool = discover_all_functions()

        if not _functions_pool:
            # No functions found, disable plugin
            _config["enabled"] = False
            return

    # Check if this is a rerun (don't instrument on reruns)
    test_id = item.nodeid
    if test_id in _rerun_state and _rerun_state[test_id] > 0:
        # This is a rerun, don't instrument
        return

    # Get terminal reporter for output
    terminal_reporter = item.config.pluginmanager.get_plugin("terminalreporter")

    # Instrument on first test or after rotation interval
    if _test_counter == 0:
        if terminal_reporter:
            terminal_reporter.write_line(
                f"\n[libdebugger-stress] Initial instrumentation (found {len(_functions_pool)} functions)",
                green=True,
                bold=True,
            )
        _instrument_random_functions(terminal_reporter)
    elif _test_counter % _config["rotation_interval"] == 0:
        _rotate_instrumentation(terminal_reporter)

    # Start tracking this test
    if _tracker:
        _tracker.start_test(test_id)

    # Create hogtrace context for this test
    _test_context_manager = new_context()
    _test_context_manager.__enter__()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Capture test results and handle failures."""
    global _tracker, _reporter, _rerun_state

    outcome = yield
    report = outcome.get_result()

    if not _config.get("enabled"):
        return

    test_id = item.nodeid

    # Only handle call phase failures (not setup/teardown)
    if call.when == "call" and report.failed:
        # Check if instrumentation is active (regardless of whether it executed)
        # We retry any failure when instrumentation is active to rule out instrumentation bugs
        if _tracker and len(_tracker.get_active_records()) > 0:
            # Get current rerun count
            rerun_count = _rerun_state.get(test_id, 0)

            if rerun_count < _config["max_reruns"]:
                # Get terminal reporter for output
                terminal_reporter = item.config.pluginmanager.get_plugin(
                    "terminalreporter"
                )

                # Record the failure
                failure_record = _tracker.record_test_failure(
                    test_id=test_id,
                    test_name=item.name,
                    failure_message=str(report.longrepr),
                    failure_traceback=str(report.longrepr),
                    rerun_count=rerun_count,
                )

                # Get list of instrumented functions that executed
                executed_functions = _tracker.get_executed_functions()

                # Report the failure and rerun attempt
                if terminal_reporter:
                    terminal_reporter.write_line(
                        "\n[libdebugger-stress] Test FAILED with instrumentation active",
                        red=True,
                        bold=True,
                    )
                    terminal_reporter.write_line(
                        f"[libdebugger-stress] Test: {item.name}", yellow=True
                    )
                    if executed_functions:
                        terminal_reporter.write_line(
                            "[libdebugger-stress] Instrumented functions that executed:",
                            yellow=True,
                        )
                        for func_info in executed_functions[:10]:  # Show first 10
                            terminal_reporter.write_line(
                                f"  - {func_info['module']}.{func_info['qualname']}",
                                yellow=True,
                            )
                        if len(executed_functions) > 10:
                            terminal_reporter.write_line(
                                f"  ... and {len(executed_functions) - 10} more",
                                yellow=True,
                            )
                    terminal_reporter.write_line(
                        f"[libdebugger-stress] Retrying without instrumentation (attempt {rerun_count + 1}/{_config['max_reruns']})...",
                        cyan=True,
                        bold=True,
                    )

                # Increment rerun count
                _rerun_state[test_id] = rerun_count + 1

                # Clean up instrumentation for rerun
                _cleanup_instrumentation()

                # Try to rerun the test without instrumentation
                # Directly call the test function to properly detect pass/fail
                from _pytest.runner import CallInfo

                def run_test_directly():
                    """Directly run the test function to properly capture exceptions."""
                    # For function tests, call the function directly
                    if hasattr(item, "obj") and callable(item.obj):
                        # Get the test function
                        test_func = item.obj

                        # Handle unittest.TestCase methods
                        if hasattr(item, "instance") and item.instance is not None:
                            # This is a method on a test class instance
                            test_func(item.instance)
                        else:
                            # This is a standalone function
                            # Get fixtures if any
                            funcargs = getattr(item, "funcargs", {})
                            test_func(**funcargs)
                    else:
                        # Fallback to running via runtest
                        item.runtest()

                call_info = CallInfo.from_call(run_test_directly, when="call")

                if call_info.excinfo is None:
                    # Test passed without instrumentation - may be instrumentation-related
                    _tracker.mark_failure_caused_by_instrumentation(failure_record)

                    if terminal_reporter:
                        terminal_reporter.write_line(
                            "\n[libdebugger-stress] Test passed without instrumentation!",
                            green=True,
                            bold=True,
                        )
                        terminal_reporter.write_line(
                            "[libdebugger-stress] This may indicate an instrumentation bug (or test flakiness).",
                            yellow=True,
                            bold=True,
                        )

                    if _reporter:
                        _reporter.report_failure(failure_record)

                    # Mark test as passed
                    report.outcome = "passed"
                    report.longrepr = None
                    return
                else:
                    # Test still failed without instrumentation - likely genuine failure
                    if terminal_reporter:
                        terminal_reporter.write_line(
                            "\n[libdebugger-stress] Test STILL FAILED without instrumentation",
                            red=True,
                            bold=True,
                        )
                        terminal_reporter.write_line(
                            "[libdebugger-stress] This may be a genuine test failure (likely not instrumentation-related)",
                            yellow=True,
                        )
                    # Leave as failed, don't retry again
                    _rerun_state[test_id] = 0
                    return
            else:
                # Max reruns reached, treat as real failure
                terminal_reporter = item.config.pluginmanager.get_plugin(
                    "terminalreporter"
                )
                if terminal_reporter:
                    terminal_reporter.write_line(
                        f"\n[libdebugger-stress] Max reruns ({_config['max_reruns']}) reached for {item.name}",
                        red=True,
                        bold=True,
                    )
                    terminal_reporter.write_line(
                        "[libdebugger-stress] Treating as genuine test failure",
                        yellow=True,
                    )
                _rerun_state[test_id] = 0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Wrap test execution to handle reruns."""
    global _tracker, _rerun_state

    if not _config.get("enabled"):
        yield
        return

    test_id = item.nodeid
    rerun_count = _rerun_state.get(test_id, 0)

    if rerun_count > 0:
        # This is a rerun without instrumentation
        # Make sure instrumentation is cleaned up
        if _tracker:
            _cleanup_instrumentation()

    try:
        yield
    except Exception:
        # Let the exception propagate to makereport
        raise


def pytest_runtest_teardown(item, nextitem):
    """Clean up after each test."""
    global _tracker, _test_counter, _test_context_manager, _rerun_state

    if not _config.get("enabled"):
        return

    test_id = item.nodeid

    # Exit hogtrace context
    if _test_context_manager:
        try:
            _test_context_manager.__exit__(None, None, None)
        except Exception:
            pass
        _test_context_manager = None

    # End tracking for this test
    if _tracker:
        _tracker.end_test()

    # Clean up rerun state if test passed
    if test_id in _rerun_state:
        del _rerun_state[test_id]

    # Increment test counter
    _test_counter += 1


def pytest_sessionfinish(session, exitstatus):
    """Report summary at end of session."""
    global _tracker, _reporter

    if not _config.get("enabled"):
        return

    # Clean up all instrumentation
    _cleanup_instrumentation()

    # Report summary
    if _tracker and _reporter:
        summary = _tracker.get_summary()
        # Get terminal reporter for always-visible output
        terminal_reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        _reporter.report_summary(summary, terminal_reporter=terminal_reporter)

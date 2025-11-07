"""
Failure reporting for instrumentation stress testing.

Generates detailed reports when tests fail with instrumentation but pass without.
"""

from .tracker import TestFailureRecord, InstrumentationRecord


class FailureReporter:
    """Generates detailed reports for instrumentation-related test failures."""

    def __init__(self):
        self.terminal_writer = None

    def set_terminal_writer(self, terminal_writer):
        """Set pytest's terminal writer for output."""
        self.terminal_writer = terminal_writer

    def _write(self, message: str, **kwargs):
        """Write a message to terminal or stdout."""
        if self.terminal_writer:
            self.terminal_writer.write(message, **kwargs)
        else:
            print(message, end="")

    def _write_line(self, message: str = "", **kwargs):
        """Write a line to terminal or stdout."""
        if self.terminal_writer:
            self.terminal_writer.line(message, **kwargs)
        else:
            print(message)

    def _section(self, title: str, sep: str = "="):
        """Write a section header."""
        if self.terminal_writer:
            self.terminal_writer.sep(sep, title, bold=True, red=True)
        else:
            print(f"\n{sep * 70}")
            print(f" {title}")
            print(f"{sep * 70}")

    def report_failure(self, record: TestFailureRecord) -> None:
        """Generate a detailed failure report."""
        self._section("INSTRUMENTATION-RELATED FAILURE DETECTED")

        self._write_line()
        self._write_line(f"Test: {record.test_name}", bold=True, red=True)
        self._write_line(f"Test ID: {record.test_id}")
        self._write_line(f"Rerun count: {record.rerun_count}")
        self._write_line()

        self._write_line(
            "This test FAILED with instrumentation active but PASSED after removing instrumentation."
        )
        self._write_line(
            "This may indicate an instrumentation bug, though test flakiness could also be a factor."
        )
        self._write_line()

        # Report original failure
        self._section("Original Failure", "-")
        self._write_line(record.failure_message)
        self._write_line()
        self._write_line("Traceback:", bold=True)
        self._write_line(record.failure_traceback)
        self._write_line()

        # Report instrumented functions
        self._section("Instrumented Functions", "-")
        self._write_line(
            f"Total functions instrumented: {len(record.instrumented_functions)}"
        )
        self._write_line(
            f"Functions that executed: {len(record.executed_instrumented_functions)}"
        )
        self._write_line()

        # Report executed instrumented functions (most relevant)
        if record.executed_instrumented_functions:
            self._write_line(
                "EXECUTED INSTRUMENTED FUNCTIONS (most likely culprits):",
                bold=True,
                yellow=True,
            )
            self._write_line()

            for i, func_record in enumerate(record.executed_instrumented_functions, 1):
                self._report_function(func_record, f"[{i}]")
                self._write_line()

        # Report all instrumented functions
        if len(record.instrumented_functions) > len(
            record.executed_instrumented_functions
        ):
            self._write_line(
                "OTHER INSTRUMENTED FUNCTIONS (did not execute):", bold=True
            )
            self._write_line()

            non_executed = [
                rec
                for rec in record.instrumented_functions
                if rec not in record.executed_instrumented_functions
            ]

            for i, func_record in enumerate(non_executed, 1):
                self._report_function_brief(func_record, f"[{i}]")

            self._write_line()

        self._section("Recommendations", "-")
        self._write_line("1. Review the executed instrumented functions listed above")
        self._write_line(
            "2. Check if the probe code could affect the function's behavior"
        )
        self._write_line(
            "3. Verify that bytecode manipulation is correct for these functions"
        )
        self._write_line(
            "4. Consider adding these functions to an exclusion list if needed"
        )
        self._write_line()
        self._section("", "=")

    def _report_function(self, record: InstrumentationRecord, prefix: str = ""):
        """Report detailed information about an instrumented function."""
        info = record.function_info

        self._write_line(f"{prefix} Function: {info['qualname']}", bold=True, cyan=True)

        if info["module"]:
            self._write_line(f"     Module: {info['module']}")

        if info["file"] and info["line"]:
            self._write_line(f"     Location: {info['file']}:{info['line']}")
        elif info["file"]:
            self._write_line(f"     File: {info['file']}")

        self._write_line(f"     Execution count: {record.execution_count}")

        if record.probe_source:
            self._write_line("     Probe source:", yellow=True)
            for line in record.probe_source.split("\n"):
                if line.strip():
                    self._write_line(f"       {line}")

        if record.captured_data:
            self._write_line(
                f"     Captured data ({len(record.captured_data)} captures):",
                yellow=True,
            )
            for i, data in enumerate(record.captured_data[:3], 1):  # Show first 3
                self._write_line(f"       Capture {i}:")
                for key, value in data.items():
                    # Truncate long values
                    value_str = str(value)
                    if len(value_str) > 100:
                        value_str = value_str[:97] + "..."
                    self._write_line(f"         {key}: {value_str}")

            if len(record.captured_data) > 3:
                self._write_line(
                    f"       ... and {len(record.captured_data) - 3} more captures"
                )

    def _report_function_brief(self, record: InstrumentationRecord, prefix: str = ""):
        """Report brief information about an instrumented function."""
        info = record.function_info
        location = (
            f"{info['file']}:{info['line']}"
            if info["file"] and info["line"]
            else info.get("module", "unknown")
        )
        self._write_line(f"  {prefix} {info['qualname']} ({location})")

    def report_summary(self, summary: dict, terminal_reporter=None) -> None:
        """Report summary statistics at the end of the session."""
        # Use terminal_reporter directly for always-visible output
        if terminal_reporter:
            # Use terminal reporter's write_line which always prints
            terminal_reporter.write_line("")
            terminal_reporter.section(
                "INSTRUMENTATION STRESS TEST SUMMARY", "=", bold=True, cyan=True
            )
            terminal_reporter.write_line("")

            terminal_reporter.write_line("Test Execution Statistics:", bold=True)
            terminal_reporter.write_line(f"  Total tests run: {summary['total_tests']}")
            terminal_reporter.write_line(
                f"  Total functions instrumented: {summary['total_instrumented']}"
            )
            terminal_reporter.write_line(
                f"  Functions that executed: {summary['total_executed']}"
            )

            # Calculate execution rate
            if summary["total_instrumented"] > 0:
                exec_rate = (
                    summary["total_executed"] / summary["total_instrumented"]
                ) * 100
                terminal_reporter.write_line(f"  Execution rate: {exec_rate:.1f}%")

            terminal_reporter.write_line("")

            # Failure statistics
            terminal_reporter.write_line("Failure Statistics:", bold=True)
            terminal_reporter.write_line(
                f"  Total test failures: {summary['total_failures']}"
            )

            # Highlight possible instrumentation bugs
            if summary["failures_caused_by_instrumentation"] > 0:
                terminal_reporter.write_line(
                    f"  Possible instrumentation bugs: {summary['failures_caused_by_instrumentation']}",
                    bold=True,
                    red=True,
                )
            else:
                terminal_reporter.write_line(
                    f"  Possible instrumentation bugs: {summary['failures_caused_by_instrumentation']}",
                    bold=True,
                    green=True,
                )

            terminal_reporter.write_line("")

            # Show result interpretation
            if summary["failures_caused_by_instrumentation"] > 0:
                terminal_reporter.write_line(
                    "Result: Possible instrumentation bugs detected!",
                    bold=True,
                    red=True,
                )
                terminal_reporter.write_line(
                    "(Tests that passed after removing instrumentation - may indicate bugs or flakiness)",
                    yellow=True,
                )
            else:
                terminal_reporter.write_line(
                    "Result: No instrumentation bugs detected!",
                    bold=True,
                    green=True,
                )
                if summary["total_failures"] > 0:
                    terminal_reporter.write_line(
                        "(All failures were genuine test failures, not caused by instrumentation)",
                        yellow=True,
                    )

            terminal_reporter.write_line("")

            if summary["instrumentation_caused_failures"]:
                terminal_reporter.write_line(
                    "Tests with possible instrumentation bugs:", bold=True, red=True
                )
                for record in summary["instrumentation_caused_failures"]:
                    terminal_reporter.write_line(f"  - {record.test_name}")
                terminal_reporter.write_line("")

            terminal_reporter.section("", "=", bold=True, cyan=True)
            terminal_reporter.write_line("")
        else:
            # Fallback to self._write_line for when terminal_reporter is not available
            self._section("INSTRUMENTATION STRESS TEST SUMMARY")

            self._write_line()
            self._write_line("Test Execution Statistics:", bold=True)
            self._write_line(f"  Total tests run: {summary['total_tests']}")
            self._write_line(
                f"  Total functions instrumented: {summary['total_instrumented']}"
            )
            self._write_line(f"  Functions that executed: {summary['total_executed']}")

            # Calculate execution rate
            if summary["total_instrumented"] > 0:
                exec_rate = (
                    summary["total_executed"] / summary["total_instrumented"]
                ) * 100
                self._write_line(f"  Execution rate: {exec_rate:.1f}%")

            self._write_line()

            # Failure statistics
            self._write_line("Failure Statistics:", bold=True)
            self._write_line(f"  Total test failures: {summary['total_failures']}")

            # Highlight possible instrumentation bugs
            if summary["failures_caused_by_instrumentation"] > 0:
                self._write_line(
                    f"  Possible instrumentation bugs: {summary['failures_caused_by_instrumentation']}",
                    bold=True,
                    red=True,
                )
            else:
                self._write_line(
                    f"  Possible instrumentation bugs: {summary['failures_caused_by_instrumentation']}",
                    bold=True,
                    green=True,
                )

            self._write_line()

            # Show result interpretation
            if summary["failures_caused_by_instrumentation"] > 0:
                self._write_line(
                    "Result: Possible instrumentation bugs detected!",
                    bold=True,
                    red=True,
                )
                self._write_line(
                    "(Tests that passed after removing instrumentation - may indicate bugs or flakiness)",
                    yellow=True,
                )
            else:
                self._write_line(
                    "Result: No instrumentation bugs detected!",
                    bold=True,
                    green=True,
                )
                if summary["total_failures"] > 0:
                    self._write_line(
                        "(All failures were genuine test failures, not caused by instrumentation)",
                        yellow=True,
                    )

            self._write_line()

            if summary["instrumentation_caused_failures"]:
                self._write_line(
                    "Tests with possible instrumentation bugs:", bold=True, red=True
                )
                for record in summary["instrumentation_caused_failures"]:
                    self._write_line(f"  - {record.test_name}")
                self._write_line()

            self._section("", "=")

    def report_test_starting(self, test_name: str, num_instrumented: int) -> None:
        """Report when a test starts with instrumentation."""
        # Only report in verbose mode
        if (
            self.terminal_writer
            and hasattr(self.terminal_writer, "_tw")
            and self.terminal_writer._tw.hasmarkup
        ):
            # Verbose mode
            pass  # Could add verbose output here

    def report_rerun(self, test_name: str, rerun_count: int) -> None:
        """Report that a test is being rerun without instrumentation."""
        self._write_line()
        self._write_line(
            f"Rerunning {test_name} without instrumentation (attempt {rerun_count})...",
            bold=True,
            yellow=True,
        )

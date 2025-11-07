"""
Instrumentation state tracking for stress testing.

This module tracks which functions are instrumented, whether they executed,
and captures data for failure reporting.
"""

from typing import Dict, Set, Callable, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class InstrumentationRecord:
    """Record of a function's instrumentation."""

    function: Callable
    function_info: dict
    decorator: Any  # InstrumentationDecorator instance
    instrumented_at: datetime = field(default_factory=datetime.now)
    executed: bool = False
    execution_count: int = 0
    captured_data: List[dict] = field(default_factory=list)
    probe_source: str = ""


@dataclass
class TestFailureRecord:
    """Record of a test failure with instrumentation."""

    test_id: str
    test_name: str
    failure_message: str
    failure_traceback: str
    instrumented_functions: List[InstrumentationRecord]
    executed_instrumented_functions: List[InstrumentationRecord]
    rerun_count: int = 0
    passed_without_instrumentation: bool = False


class InstrumentationTracker:
    """
    Tracks instrumentation state across test execution.

    This class maintains:
    - Which functions are currently instrumented
    - Which instrumented functions were executed
    - Captured data from probe execution
    - Test failure records
    """

    def __init__(self):
        self.records: Dict[int, InstrumentationRecord] = {}  # func_id -> record
        self.active_instrumentations: Set[int] = set()  # func_id set
        self.captured_events: List[dict] = []  # All captured events
        self.current_test_id: Optional[str] = None
        self.failure_records: List[TestFailureRecord] = []
        self.stats = {
            "total_instrumented": 0,
            "total_executed": 0,
            "total_tests": 0,
            "failures_with_instrumentation": 0,
            "failures_caused_by_instrumentation": 0,
        }

    def add_instrumentation(
        self,
        func: Callable,
        decorator: Any,
        function_info: dict,
        probe_source: str = "",
    ) -> None:
        """Register a new instrumentation."""
        func_id = id(func)

        record = InstrumentationRecord(
            function=func,
            function_info=function_info,
            decorator=decorator,
            probe_source=probe_source,
        )

        self.records[func_id] = record
        self.active_instrumentations.add(func_id)
        self.stats["total_instrumented"] += 1

    def mark_executed(
        self, func: Callable, captured_data: Optional[dict] = None
    ) -> None:
        """Mark that an instrumented function was executed."""
        func_id = id(func)

        if func_id in self.records:
            record = self.records[func_id]
            if not record.executed:
                record.executed = True
                self.stats["total_executed"] += 1
            record.execution_count += 1

            if captured_data:
                record.captured_data.append(captured_data)

    def capture_event(self, event_data: dict) -> None:
        """Capture an event from instrumentation."""
        event_data["timestamp"] = datetime.now().isoformat()
        event_data["test_id"] = self.current_test_id
        self.captured_events.append(event_data)

    def get_active_records(self) -> List[InstrumentationRecord]:
        """Get all active instrumentation records."""
        return [
            self.records[func_id]
            for func_id in self.active_instrumentations
            if func_id in self.records
        ]

    def get_executed_records(self) -> List[InstrumentationRecord]:
        """Get all records for functions that executed."""
        return [record for record in self.get_active_records() if record.executed]

    def get_executed_functions(self) -> List[dict]:
        """Get function info for all functions that executed."""
        return [record.function_info for record in self.get_executed_records()]

    def has_executed_instrumentation(self) -> bool:
        """Check if any instrumented function was executed during current test."""
        return len(self.get_executed_records()) > 0

    def cleanup_instrumentation(self, func: Callable) -> None:
        """Clean up instrumentation for a specific function."""
        func_id = id(func)

        if func_id in self.records:
            record = self.records[func_id]

            # Call cleanup on the decorator
            try:
                record.decorator.cleanup()
            except Exception as e:
                print(
                    f"Warning: Failed to cleanup instrumentation for {record.function_info['name']}: {e}"
                )

            # Remove from active set
            self.active_instrumentations.discard(func_id)

    def cleanup_all(self) -> None:
        """Clean up all active instrumentations."""
        for func_id in list(self.active_instrumentations):
            if func_id in self.records:
                record = self.records[func_id]
                try:
                    record.decorator.cleanup()
                except Exception as e:
                    print(f"Warning: Failed to cleanup instrumentation: {e}")

        self.active_instrumentations.clear()
        self.records.clear()

    def reset_execution_tracking(self) -> None:
        """Reset execution tracking for a new test."""
        for record in self.records.values():
            record.executed = False
            record.captured_data.clear()

    def record_test_failure(
        self,
        test_id: str,
        test_name: str,
        failure_message: str,
        failure_traceback: str,
        rerun_count: int = 0,
    ) -> TestFailureRecord:
        """Record a test failure with instrumentation information."""
        instrumented = self.get_active_records()
        executed = self.get_executed_records()

        record = TestFailureRecord(
            test_id=test_id,
            test_name=test_name,
            failure_message=failure_message,
            failure_traceback=failure_traceback,
            instrumented_functions=instrumented.copy(),
            executed_instrumented_functions=executed.copy(),
            rerun_count=rerun_count,
        )

        self.failure_records.append(record)
        self.stats["failures_with_instrumentation"] += 1

        return record

    def mark_failure_caused_by_instrumentation(self, record: TestFailureRecord) -> None:
        """Mark that a failure was caused by instrumentation."""
        record.passed_without_instrumentation = True
        self.stats["failures_caused_by_instrumentation"] += 1

    def start_test(self, test_id: str) -> None:
        """Mark the start of a test."""
        self.current_test_id = test_id
        self.reset_execution_tracking()
        self.stats["total_tests"] += 1

    def end_test(self) -> None:
        """Mark the end of a test."""
        self.current_test_id = None

    def get_summary(self) -> dict:
        """Get summary statistics."""
        return {
            **self.stats,
            "active_instrumentations": len(self.active_instrumentations),
            "total_failures": len(self.failure_records),
            "instrumentation_caused_failures": [
                rec
                for rec in self.failure_records
                if rec.passed_without_instrumentation
            ],
        }

    def get_function_report(self, record: InstrumentationRecord) -> str:
        """Generate a detailed report for an instrumented function."""
        info = record.function_info
        lines = []

        lines.append(f"Function: {info['qualname']}")
        if info["module"]:
            lines.append(f"  Module: {info['module']}")
        if info["file"]:
            lines.append(f"  File: {info['file']}:{info['line']}")
        lines.append(f"  Executed: {record.executed}")
        lines.append(f"  Execution count: {record.execution_count}")

        if record.probe_source:
            lines.append("  Probe source:")
            for line in record.probe_source.split("\n"):
                lines.append(f"    {line}")

        if record.captured_data:
            lines.append(f"  Captured data ({len(record.captured_data)} captures):")
            for i, data in enumerate(record.captured_data[:5], 1):  # Show first 5
                lines.append(f"    Capture {i}: {data}")
            if len(record.captured_data) > 5:
                lines.append(f"    ... and {len(record.captured_data) - 5} more")

        return "\n".join(lines)

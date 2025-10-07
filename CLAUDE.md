# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`libdebugger` is a Python library that implements runtime bytecode instrumentation for live debugging. It integrates with PostHog to enable dynamic breakpoints that can be set remotely and capture local variables and stack traces at runtime without stopping execution.

## Core Architecture

The library uses Python bytecode manipulation to inject breakpoint handlers into running code:

1. **LiveDebuggerManager** (`manager.py`) - Main orchestrator that polls PostHog API for breakpoint configurations and coordinates instrumentation/de-instrumentation
2. **Instrumentation layer** (`instrumentation.py`) - Handles bytecode manipulation using the `bytecode` library to inject breakpoint handler calls at specific line numbers
3. **File utilities** (`file_utils.py`) - Locates function objects from filename/line number pairs by finding modules in `sys.modules` and parsing AST
4. **Breakpoint model** (`breakpoint.py`) - Data class representing breakpoint configuration with UUID, filename, line number, and optional conditional expression

### How Instrumentation Works

- The manager maintains a mapping of `(filename, lineno)` → `bid` (breakpoint ID)
- When breakpoints are fetched, functions are located via AST parsing and `sys.modules` lookup
- Original function bytecode is saved in `__posthog_original_code` attribute before modification
- Bytecode is manipulated to inject calls to `__posthog_ykwdzsgtgp_breakpoint_handler` at target lines
- The handler captures local variables (serialized with jsonpickle), stack traces, and sends them to PostHog as `$data_breakpoint_hit` events
- Python 3.10+ uses different bytecode instructions (PRECALL/CALL) vs older versions (CALL_FUNCTION)

### Key Implementation Details

- **Breakpoint registry**: Global `_BREAKPOINTS` dict maps `bid` → list of `Breakpoint` objects (multiple breakpoints can target same line)
- **Deduplication**: Multiple breakpoints at the same file position share a single bytecode instrumentation
- **Polling**: Manager uses PostHog's `Poller` class to periodically fetch breakpoint updates
- **Reset logic**: When breakpoints change, affected functions are reset and re-instrumented (not optimized yet - see TODO in manager.py:92)
- **Nested functions**: Closures are instrumented recursively by detecting `LOAD_CONST` instructions with code objects

## Development Commands

This project uses `uv` for dependency management. The project requires Python 3.11+.

### Setup
```bash
uv sync
```

### Linting
```bash
uv run ruff check .
uv run ruff format .
```

### Running the example
```bash
uv run python main.py
```

## Dependencies

- **bytecode**: Core library for Python bytecode manipulation
- **jsonpickle**: Serializes local variables for transmission to PostHog
- **posthog**: PostHog Python SDK for event capture and API polling
- **hypothesis**: Property-based testing framework (dev dependency)
- **ruff**: Fast Python linter and formatter (dev dependency)

## Important Constraints

- The conditional expression feature (`breakpoint.condition_matches()`) is stubbed - it currently ignores conditions and always returns True
- Expression evaluation (`expression.py`) is not implemented
- Closures retain instrumentation even after parent functions are reset (see instrumentation.py:138)
- No test suite exists yet (hypothesis is installed but unused)

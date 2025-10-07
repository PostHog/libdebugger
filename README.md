# libdebugger

A Python library that enables live debugging in production through runtime bytecode instrumentation. Set breakpoints remotely via PostHog and capture local variables, stack traces, and execution context without stopping your application.

## What is this?

`libdebugger` allows you to debug running Python applications by dynamically injecting breakpoints at specific file locations. When a breakpoint is hit:

- Local variables are captured and serialized
- Full stack traces are recorded
- Data is sent to PostHog as events
- **Your application continues running** - no interruption to execution

This is particularly useful for debugging issues in production environments where traditional debugging isn't feasible.

## How it works

The library uses Python bytecode manipulation to inject breakpoint handlers into your running code:

1. The `LiveDebuggerManager` polls the PostHog API for breakpoint configurations
2. When breakpoints are added/updated, the library locates the target functions using AST parsing
3. Function bytecode is instrumented to call a handler at the specified line number
4. When execution reaches that line, local variables and stack traces are captured and sent to PostHog
5. Breakpoints can be added, removed, or updated dynamically without restarting your application

## Requirements

- Python 3.11 or higher
- PostHog account with a personal API key

## Installation

This project uses [uv](https://github.com/astral-sh/uv) for dependency management:

```bash
# Install dependencies
uv sync

# Or install as a package
uv pip install -e .
```

### Dependencies

- `bytecode` - Python bytecode manipulation
- `jsonpickle` - Serialization of local variables
- `posthog` - PostHog Python SDK

## Quick Start

```python
from posthog import Posthog
from libdebugger import LiveDebuggerManager

# Initialize PostHog client with your API key and personal API key
posthog_client = Posthog(
    api_key='your-project-api-key',
    personal_api_key='your-personal-api-key',
    host='https://app.posthog.com'
)

# Create and start the debugger manager
debugger = LiveDebuggerManager(
    client=posthog_client,
    poll_interval=30  # Poll for breakpoint updates every 30 seconds
)

debugger.start()

# Your application code runs here
# Breakpoints will be automatically applied based on PostHog configuration

# When done:
debugger.stop()
```

## Usage

### Creating Breakpoints

Breakpoints are managed through the PostHog API. Create breakpoints by specifying:

- `filename`: Absolute or relative path to the Python file
- `line_number`: Line number where the breakpoint should trigger
- `condition`: (Optional) Conditional expression - currently not implemented

### Breakpoint Data

When a breakpoint is hit, the following data is captured and sent to PostHog as a `$data_breakpoint_hit` event:

- `$breakpoint_id`: Unique identifier for the breakpoint
- `$file_path`: File where the breakpoint was hit
- `$line_number`: Line number
- `$locals_variables`: All local variables at that point (serialized)
- `$stack_trace`: Full stack trace showing the call chain
- `$timestamp`: When the breakpoint was hit
- `$thread_id`: Thread identifier
- `$thread_name`: Thread name

### API Reference

#### LiveDebuggerManager

**Constructor:**
```python
LiveDebuggerManager(client: Posthog, poll_interval: int = 30)
```
- `client`: PostHog client instance with `personal_api_key` configured
- `poll_interval`: Seconds between polling for breakpoint updates (default: 30)

**Methods:**
- `start()`: Begin polling for breakpoints and enable live debugging
- `stop()`: Stop polling and remove all active breakpoints

#### Breakpoint

```python
Breakpoint(
    uuid: str,
    filename: str,
    lineno: int,
    conditional_expr: Optional[str] = None
)
```

## Development

### Linting and Formatting

```bash
# Check code with ruff
uv run ruff check .

# Format code
uv run ruff format .
```

### Running the Example

```bash
uv run python main.py
```

## How Breakpoints Are Applied

1. **Function Discovery**: The library finds the function containing the target line by:
   - Locating the module in `sys.modules` that matches the filename
   - Parsing the module's AST to find the function containing the line number

2. **Bytecode Instrumentation**: The function's bytecode is modified to inject a call to the breakpoint handler at the target line

3. **Original Code Preservation**: The original bytecode is saved in `__posthog_original_code` attribute for later restoration

4. **Dynamic Updates**: When breakpoints change, functions are de-instrumented and re-instrumented automatically

## Limitations

- Conditional breakpoints are not yet implemented (conditions are ignored)
- Expression evaluation is stubbed
- Nested functions (closures) may retain instrumentation after parent function reset
- Only works with modules already loaded in `sys.modules`

## Architecture

See [CLAUDE.md](./CLAUDE.md) for detailed architecture documentation.

## License

[Add your license here]

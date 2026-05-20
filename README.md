# libdebugger

> [!WARNING]
> This is an **EXPERIMENTAL** library not ready for production use. For internal use only (for now).

A Python library that enables live debugging in production through PEP 669 `sys.monitoring` events.
Set breakpoints remotely via PostHog and capture local variables, stack traces, and execution context
without stopping your application. Requires Python 3.12+.

## Test

You can run tests for all supported versions using `tox`, just run:

```shell
$ tox
```

You can also run tests for a particular version with `uv` directly

```shell
$ uv run --python 3.12 pytest test/
```

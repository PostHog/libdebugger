# libdebugger

A Python library that enables live debugging in production through runtime bytecode instrumentation. Set breakpoints remotely 
via PostHog and capture local variables, stack traces, and execution context without stopping your application.

## Test

You can run tests for all supported versions using `tox`, just run:

```shell
$ tox
```

You can also run tests for a particular version with `uv` directly

```shell
$ uv run --python 3.11 pytest test/
```
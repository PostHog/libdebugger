"""
Exit-probe coverage gaps — payload fidelity, defensive frame-mismatch, and
non-Exception exit paths.

Companion to ``test_manager_probe_firing.py`` (which pins down "exit fires"
counts but not "exit sees the right values"). The cases here cover:

* ``retval`` is the actual return value the user code produced.
* ``exception`` is the actual exception object the user code raised.
* The wrapper's defensive branch — when ``instrumented_fn`` somehow
  fails to push its frame, exit probes must NOT fire and the stack
  must be left intact for whoever truly owns the top frame.
* ``BaseException`` (KeyboardInterrupt) still triggers exit probes —
  the wrapper catches ``BaseException``, not just ``Exception``.
* Generator functions: calling one returns a generator object without
  running the body; the wrapper sees one entry+exit per call and
  ``retval`` is the generator object.
"""

from __future__ import annotations

import types

import pytest
from hogtrace.context import new_context

import libdebugger.instrumentation as instr
from test._manager_helpers import _build_program, target_mod


@pytest.fixture
def hogtrace_scope():
    with new_context():
        yield


@pytest.fixture
def capture_enqueue(monkeypatch):
    calls = []

    def _stub(program, probe, captures):
        calls.append((program, probe, captures))

    monkeypatch.setattr(instr, "_enqueue_message", _stub)
    return calls


# ---------------------------------------------------------------------------
# Payload fidelity — retval and exception are bound to what user code did.
# ---------------------------------------------------------------------------


def test_exit_probe_sees_actual_retval(hogtrace_scope, capture_enqueue):
    """Exit probe's ``capture(rv=retval)`` records the user-visible return value.

    The headline ``exit fires once`` test only counts firings; if the
    wrapper accidentally passed ``retval=None`` (or some sentinel) into
    ``_run_probes``, that count test would still pass. This case asserts
    on the captured value so the regression is loud.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_a:exit { capture(rv=retval); }",
        program_id="prog-retval",
    )
    install_program(program)

    result = target_mod.fn_a(7)
    assert result == 1 + 2 + 7  # behavior preservation cross-check

    assert len(capture_enqueue) == 1
    _prog, probe, captures = capture_enqueue[0]
    assert probe.spec.target == "exit"
    # The captured retval must match what the user code actually returned.
    assert captures.get("rv") == 10, f"retval payload mismatch: {captures!r}"


def test_run_probes_kwargs_on_raise_path(hogtrace_scope, monkeypatch):
    """Spy on ``_run_probes`` to pin its kwargs on the raise path.

    The hogtrace VM treats ``retval=None`` as *no return value* (the
    ``Optional[Any] = None`` sentinel is overloaded). So we can't probe
    "retval is None on raise" through a hogtrace predicate — that path
    raises ``Predicate error: No return value``. Instead, assert at the
    Python boundary that the wrapper hands ``_run_probes`` exactly
    ``retval=None, exception=<the raised exception>`` for the exit slot
    on the raise path.

    Without this guard a future change that (e.g.) reused the previous
    call's ``retval`` could silently bleed values across raises.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_raises:exit { capture(hit=1); }",
        program_id="prog-spy",
    )
    install_program(program)

    spy_calls = []
    real_run_probes = instr._run_probes

    def spy(probes, frame, **kwargs):
        spy_calls.append((probes, frame, kwargs))
        return real_run_probes(probes, frame, **kwargs)

    monkeypatch.setattr(instr, "_run_probes", spy)

    with pytest.raises(ValueError, match="boom") as excinfo:
        target_mod.fn_raises()

    # Filter to the exit-slot call: entry-side _run_probes is invoked
    # inside instrumented_fn (so it's also recorded), but only the exit
    # call carries retval/exception kwargs.
    exit_calls = [c for c in spy_calls if "retval" in c[2] or "exception" in c[2]]
    assert len(exit_calls) == 1, (
        f"expected exactly one exit-slot _run_probes call; got {len(exit_calls)}: "
        f"{[c[2] for c in exit_calls]}"
    )
    _probes, _frame, kwargs = exit_calls[0]
    assert kwargs.get("retval") is None, (
        f"raise path must pass retval=None to _run_probes; got {kwargs!r}"
    )
    assert kwargs.get("exception") is excinfo.value, (
        f"raise path must pass the raised exception by identity; "
        f"got {kwargs.get('exception')!r} vs raised {excinfo.value!r}"
    )


def test_exit_probe_sees_actual_exception(hogtrace_scope, capture_enqueue):
    """Exit probe's ``capture(args=exception.args)`` records the raised exception.

    ``hogtrace`` serializes exception objects opaquely (their direct ``capture``
    rendering is ``'{}'``), but attribute access through ``exception.args``
    returns the args tuple as a string. That's enough to prove the wrapper
    passed the *actual* exception object (with its args) rather than a
    placeholder or the wrong exception.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_raises:exit / exception != None / "
        "{ capture(args=exception.args); }",
        program_id="prog-exc-args",
    )
    install_program(program)

    with pytest.raises(ValueError, match="boom"):
        target_mod.fn_raises()

    assert len(capture_enqueue) == 1
    _prog, probe, captures = capture_enqueue[0]
    assert probe.spec.target == "exit"
    # ``fn_raises`` raises ValueError("boom") -> args == ("boom",).
    assert captures.get("args") == "('boom',)", (
        f"exception.args payload mismatch: {captures!r}"
    )


def test_exit_probe_exception_none_on_normal_return(hogtrace_scope, capture_enqueue):
    """On a normal return, ``exception`` is None — the ``exception == None``
    predicate must let the capture through.

    Mirror of ``test_exit_probe_retval_none_when_function_raises``: this is
    the other half of the contract. Without it, a wrapper that forwarded a
    stale exception from a prior raising call would never get caught.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_a:exit / exception == None / { capture(ok=1); }",
        program_id="prog-exc-none",
    )
    install_program(program)

    assert target_mod.fn_a(0) == 3
    assert len(capture_enqueue) == 1
    _prog, probe, captures = capture_enqueue[0]
    assert probe.spec.target == "exit"
    assert captures.get("ok") == 1


# ---------------------------------------------------------------------------
# Defensive branch — instrumented_fn fails to push its frame.
# ---------------------------------------------------------------------------


def test_exit_probe_skipped_when_instrumented_fn_did_not_push_frame(
    hogtrace_scope, capture_enqueue
):
    """The wrapper's ``function_frame is previous_frame_top`` branch.

    Normal flow: instrumented_fn pushes its frame via the bytecode-injected
    entry call, then runs the body. If instrumented_fn somehow fails *before*
    that push, the only frame on ``self.frames`` belongs to whichever outer
    call last pushed (or it's missing entirely). In that case the wrapper
    must NOT pop somebody else's frame and must NOT fire exit probes against
    it — exit probes belong to the call that pushed.

    We force the failure path by monkey-patching ``instrumented_fn`` after
    install to a function that raises without pushing. We also pre-seed
    ``decorator.frames`` with a sentinel so we can verify the wrapper
    restored the stack (it must look identical pre-and-post call).
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_a:entry { capture(e=1); }\n"
        "fn:test.target.fn_a:exit { capture(x=1); }",
        program_id="prog-pre-push-crash",
    )
    install_program(program)

    decorator = target_mod.fn_a.__posthog_decorator
    assert decorator is not None

    # Seed a sentinel frame on the stack so we can detect tampering. The
    # wrapper's ``previous_frame_top`` capture must read this, see the
    # popped frame is the same object (because no push happened), and
    # take the "restore + don't fire" branch.
    sentinel_frame = types.SimpleNamespace(name="sentinel-not-a-real-frame")
    decorator.frames.append(sentinel_frame)  # type: ignore[arg-type]

    # Replace instrumented_fn with one that raises *without* pushing a
    # frame onto decorator.frames. This is the exact failure mode the
    # defensive branch is written to handle.
    def crashes_before_push(*_args, **_kwds):
        raise RuntimeError("pre-push crash")

    decorator.instrumented_fn = crashes_before_push  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="pre-push crash"):
        target_mod.fn_a(0)

    # Invariant 1: the sentinel frame is still on the stack — the wrapper
    # popped it (because frames.pop is unconditional), saw it equaled
    # previous_frame_top, and pushed it back via the defensive branch.
    assert decorator.frames == [sentinel_frame], (
        f"wrapper failed to restore the stack on pre-push crash: "
        f"frames={decorator.frames!r}"
    )

    # Invariant 2: no probes fired. Entry would have, but the injected
    # entry call lives inside instrumented_fn — which we replaced. Exit
    # must skip because the popped frame doesn't belong to this call.
    assert capture_enqueue == [], (
        f"no probe should fire when instrumented_fn crashed pre-push; got "
        f"{[(c[1].spec.target, c[2]) for c in capture_enqueue]}"
    )


# ---------------------------------------------------------------------------
# BaseException — KeyboardInterrupt / SystemExit / GeneratorExit also fire exit.
# ---------------------------------------------------------------------------


def test_exit_probe_fires_on_keyboard_interrupt(hogtrace_scope, capture_enqueue):
    """KeyboardInterrupt (a BaseException, not Exception) still triggers exit.

    The wrapper deliberately catches ``BaseException`` so probes still see
    the call when an interpreter signal propagates. If someone tightens
    that to ``except Exception``, this test catches the regression.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_kbd:exit / exception != None / "
        "{ capture(args=exception.args); }",
        program_id="prog-kbd",
    )
    install_program(program)

    with pytest.raises(KeyboardInterrupt):
        target_mod.fn_kbd()

    assert len(capture_enqueue) == 1, (
        f"exit must fire on BaseException; got {len(capture_enqueue)} fires"
    )
    _prog, probe, captures = capture_enqueue[0]
    assert probe.spec.target == "exit"
    assert captures.get("args") == "('interrupt',)"


# ---------------------------------------------------------------------------
# Generator functions — calling returns a generator object without running body.
# ---------------------------------------------------------------------------


def test_generator_function_v1_probe_behavior(hogtrace_scope, capture_enqueue):
    """Generator functions: NO probes fire at construction; entry fires once
    when the generator is first advanced; exit NEVER fires.

    This pins the v1 quirk so a future refactor doesn't change it silently:

    * ``target_mod.fn_gen(3)`` calls the wrapper's ``__call__``. The
      wrapper invokes ``instrumented_fn(3)``, which is itself a generator
      function — Python returns a fresh generator object without running
      the body. No frame is pushed onto the decorator's stack, so the
      wrapper's exit-finally hits the "frame missing" branch and skips
      exit probes. Result: no entry, no exit at construction.
    * The body (including the bytecode-injected entry call) only runs
      when something iterates the generator. The injected call is the
      first instruction, so entry fires exactly once on the first
      ``next()`` — subsequent ``next()`` calls resume past it.
    * Exit probes are *never* called for generator functions in v1.
      They live in the wrapper's ``finally``, which already returned
      when the wrapper handed the generator object back to the caller.

    If v2 grows generator-aware tracing, this test will fail loudly and
    flag the contract change for review.
    """
    from libdebugger.manager import install_program

    program = _build_program(
        "fn:test.target.fn_gen:entry { capture(hit=1); }\n"
        "fn:test.target.fn_gen:exit { capture(hit=2); }",
        program_id="prog-gen",
    )
    install_program(program)

    # Phase 1: construction. Calling the generator function returns a
    # generator object without running the body — no probes fire.
    gen = target_mod.fn_gen(3)
    assert capture_enqueue == [], (
        f"no probes should fire when constructing a generator; got "
        f"{[c[1].spec.target for c in capture_enqueue]}"
    )
    assert hasattr(gen, "__next__"), f"expected a generator, got {type(gen)!r}"

    # Phase 2: drain. Iterating runs the body — entry fires once on the
    # first next(); exit still never fires.
    assert list(gen) == [0, 1, 2]
    targets = [c[1].spec.target for c in capture_enqueue]
    assert targets == ["entry"], (
        f"iterating a generator should fire entry exactly once and no exit; "
        f"got {targets}"
    )

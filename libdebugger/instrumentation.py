import datetime
import threading
import sys
from typing import Callable, Final, Optional, Dict, List, Any, Set, Tuple
from types import CodeType, FrameType, FunctionType

from hogtrace import Probe, Program, execute_probe, get_store, get_scope, ProbeSpec

from libdebugger.bytecode import (
    EntrypointInjector,
    generate_code_call_self_method,
    redirector_code,
)


class InstrumentationDecorator:
    """
    InstrumentationDecorator enables dynamic entry/exit probes on functions.

    Unlike standard decorators, this works on ALL references to a function, even those
    created before instrumentation, by replacing the original function's bytecode with
    a redirect to the decorator.

    Architecture:

        func = InstrumentationDecorator(func, entry_probes, exit_probes)

        Before:                         After:
        ┌─────────────┐                ┌─────────────┐
        │   func()    │                │   func()    │ (original reference)
        │             │                │             │
        │  original   │                │  redirect   │───┐
        │  bytecode   │                │  bytecode   │   │
        └─────────────┘                └─────────────┘   │
                                                         │
       old_ref = func                  old_ref = func    │ ALL references
                                                         │ redirect through
                                                         │ decorator
                                                         │
                                                         ▼
                                             ┌────────────────────┐
                                             │    Decorator       │
                                             │   __call__()       │
                                             └────────────────────┘
                                                         │
                                                         │ calls
                                                         ▼
                                             ┌────────────────────┐
                                             │  instrumented_fn   │
                                             │                    │
                                             │ original bytecode  │
                                             │ + entry probes     │
                                             └────────────────────┘

    Flow:
      1. Entry: Bytecode injection captures frame and runs entry probes
      2. Execution: Original function body executes
      3. Exit: Decorator's try/finally runs exit probes with captured frame

    This ensures exit probes execute on both normal returns and exceptions,
    without complex bytecode manipulation of exception handling.
    """

    # The wrapped function, after __init__ this function will contain the
    # redirector_code.
    wrapped_fn: Final[Callable[..., Any]]

    # Original code of the function, so we can replace it before being deleted
    original_code: Final[CodeType]
    # Code for the redirector function, the original function code gets change to
    # redirect to the decorator, this makes sure that all instances go through here,
    # even if they were created before the decorator was created.
    redirector_code: Final[CodeType]
    # The original code instrumented with entry and line probes.
    instrumented_fn: FunctionType

    entry_probes: Set[Tuple[Program, Probe]]
    exit_probes: Set[Tuple[Program, Probe]]

    # Stack of frames, so we can properly evaluate the exit probes in the context of the function even after exit.
    frames: List[FrameType]

    def __init__(
        self,
        fn: Callable[..., Any],
        entry_probes: Set[Tuple[Program, Probe]],
        exit_probes: Set[Tuple[Program, Probe]],
    ) -> None:
        # TODO(Marce): Refuse to add decorator to a function that has already been decorated with the
        # instrumentation decorator.
        self.entry_probes = entry_probes
        self.exit_probes = exit_probes
        self.original_code = fn.__code__
        self.wrapped_fn = fn
        instrumented_code = self._instrument_frame_capture_and_entry_probes()
        self.instrumented_fn = FunctionType(
            instrumented_code,
            fn.__globals__,
            fn.__name__,
            fn.__defaults__,
            fn.__closure__,
        )
        self.redirector_code = self._generate_redirector_code()
        self.wrapped_fn.__code__ = self.redirector_code
        self.frames = []

    def __getattr__(self, name: str) -> Any:
        """
        Fallback to the real function methods by default
        """
        return getattr(self.wrapped_fn, name)

    def __del__(self):
        """
        If we are going to die, we need to restore the
        """
        self.cleanup()

    def cleanup(self):
        """
        Return the wrapped function to the original state
        """
        self.wrapped_fn.__code__ = self.original_code

    def add_probe(self, probe: Probe):
        """
        Adds an entry probe to the
        """
        # if it's an entry probe, just add to entry probes
        # else if it's a line probe, we need to instrument the bytecode directly
        pass

    def remove_probe(self, probe: Probe):
        pass

    def _push_frame(self, frame: FrameType) -> None:
        """
        This method will be called from inside the function at the start of the function
        to internally store the frame, this way we will have it available on return/exception
        to execute the probe inside the wrapped_fn frame.
        """
        self.frames.append(frame)

    def _generate_redirector_code(self) -> CodeType:
        """Generate redirector bytecode that loads decorator from constants"""
        # Create a new redirector code to self
        new_bc = redirector_code(self)
        new_bc.name = self.original_code.co_name
        new_bc.filename = self.original_code.co_filename
        new_bc.first_lineno = self.original_code.co_firstlineno

        # Preserve freevars and cellvars from original function so code assignment works
        # (even though redirector doesn't actually use them)
        new_bc.freevars = list(self.original_code.co_freevars)
        new_bc.cellvars = list(self.original_code.co_cellvars)

        return new_bc.to_code()

    def _instrument_frame_capture_and_entry_probes(self) -> CodeType:
        """
        This function does runtime bytecode manipulation on the wrapped function
        to call `_capture_caller_frame_and_run_entry_probes` on this same instance as soon
        as the function starts.
        """
        code = self.wrapped_fn.__code__
        injector = EntrypointInjector(
            code_generator=generate_code_call_self_method(
                self, "_capture_caller_frame_and_run_entry_probes"
            ),
        )

        return injector.inject(code).to_code()

    def _capture_caller_frame_and_run_entry_probes(self) -> None:
        # We run the entry probes inside another nested try..except since any
        # error in our side should NEVER disrupt the user code.
        try:
            caller_frame = sys._getframe(1)  # type: ignore
            self._push_frame(caller_frame)

            if self.entry_probes:
                for program, probe in self.entry_probes:
                    req_store = get_store()
                    assert req_store  # This is very awkward to use...
                    store = req_store.for_program(program_id=program.id)

                    captures = execute_probe(
                        program.program_bytecode, probe, caller_frame, store
                    )

                    if captures:
                        _enqueue_message(program, probe, captures)
        except Exception as e:
            print(f"EXC {e}")
            # TODO(Marce): Do something here to notify us?
            pass

    def _pop_frame(self) -> Optional[FrameType]:
        try:
            return self.frames.pop()
        except IndexError:
            return None

    def _peek_frame(self) -> Optional[FrameType]:
        try:
            return self.frames[-1]
        except IndexError:
            return None

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        # We want to make sure we are not getting the same frame we already have
        #  when we do `_pop_frame()` down below. If for any reason the function
        # crashed before we capture the frame (or while capturing the frame) then
        # we would pop a frame that is not ours. This would case wrong evaluations
        # in cases of recursive functions. To be fair, if the function crashes before
        # we are already quite doomed, but you still use toilet paper when you are sick.
        function_frame: Optional[FrameType] = None
        previous_frame = self._peek_frame()
        exception: Optional[Exception] = None
        retval: Any = None

        try:
            retval = self.instrumented_fn(*args, **kwds)
            return retval
        except Exception as e:
            exception = e
            raise
        finally:
            function_frame = self._pop_frame()

            if function_frame == previous_frame and function_frame is not None:
                # We didn't create a new frame for whatever reason! Return this one to
                # the stack and reset function_frame
                # TODO(Marce): Do something here to notify us?
                self._push_frame(function_frame)
            elif function_frame:
                try:
                    if self.exit_probes:
                        for program, probe in self.exit_probes:
                            req_store = get_store()
                            assert req_store  # This is very awkward to use...
                            store = req_store.for_program(program_id=program.id)

                            captures = execute_probe(
                                program.program_bytecode,
                                probe,
                                function_frame,
                                store,
                                retval=retval,
                                exception=exception,
                            )

                            if captures:
                                _enqueue_message(program, probe, captures)

                except Exception:
                    # TODO(Marce): Do something here to notify us?
                    pass
            else:
                # TODO(Marce): Do something here to notify us?
                pass


def _enqueue_message(program: Program, probe: Probe, captures: Dict[str, Any]):
    from posthoganalytics import capture

    scope = get_scope()
    assert scope is not None

    properties: Dict[str, Any] = {
        "program_id": program.id,
        "probe_id": probe.id,
        "context_id": scope.context_id,
        "probe_spec": serialize_probe_spec(probe.spec),
        "captures": captures,
        # "$stack_trace": stacktrace,
        "timestamp": datetime.datetime.now(),
        "thread_id": threading.current_thread().ident,
        "thread_name": threading.current_thread().name,
    }

    capture(
        "$hogtrace_capture",
        properties=properties,
        timestamp=properties["timestamp"],
    )


def serialize_probe_spec(spec: ProbeSpec) -> Dict[str, str]:
    return {
        "specifier": spec.specifier,
        "target": spec.target,
    }

import types
import datetime
import threading
import sys
import builtins
import inspect
from typing import Callable, Optional, Dict, List, Any

import jsonpickle
from bytecode import Bytecode, Instr
from libdebugger import Breakpoint, file_utils


def _serialize(v):
    return jsonpickle.dumps(v, unpicklable=True)


_BREAKPOINTS: Dict[int, List[Breakpoint]] = {}


def register_breakpoints(bid: int, bps: List[Breakpoint]):
    _BREAKPOINTS[bid] = bps


def reset_breakpoint_registry():
    _BREAKPOINTS = {}


def get_breakpoint_definition_by_bid(bid: int) -> Optional[Breakpoint]:
    return _BREAKPOINTS.get(bid)


def _breakpoint_handler(bid: int):
    try:
        breakpoint_definitions = get_breakpoint_definition_by_bid(bid)

        if not breakpoint_definitions:
            # TODO(Marce): We should remove the instrumentation, that
            # breakpoint doesnt exist anymore
            return

        # start from the frame above to not show the _breakpoint_handler
        cframe = sys._getframe().f_back
        loc = cframe.f_locals

        # If no condition matches for any breakpoint, we don't have
        # anything to do
        breakpoints_that_match = [
            bd for bd in breakpoint_definitions if bd.condition_matches(loc)
        ]
        if not breakpoints_that_match:
            return

        framestack = []
        while cframe is not None:
            framestack.append(
                (cframe.f_code.co_filename, cframe.f_code.co_name, cframe.f_lineno)
            )
            cframe = cframe.f_back

        locals_locals = {
            k: _serialize(v)
            for k, v in loc.items()
            if not (
                callable(v)  # Exclude functions
                or isinstance(v, type)  # Exclude classes
                or isinstance(v, types.ModuleType)  # Exclude modules
                or k.startswith("_")  # Exclude private/dunder
            )
        }

        for bp in breakpoints_that_match:
            _enqueue_message(bp, locals_locals, framestack)

    except Exception as e:
        print(f"Error in _breakpoint_handler: {e}")


def _enqueue_message(bp: Breakpoint, locs: Dict[str, Any], stacktrace):
    from posthoganalytics import capture, flush

    properties = {
        "$breakpoint_id": bp.uuid,
        "$file_path": bp.filename,
        "$line_number": bp.lineno,
        "$locals_variables": locs,
        "$stack_trace": stacktrace,
        "$timestamp": datetime.datetime.now(),
        "$thread_id": threading.current_thread().ident,
        "$thread_name": threading.current_thread().name,
    }

    capture(
        "$data_breakpoint_hit",
        properties=properties,
        timestamp=properties["$timestamp"],
    )
    flush()


builtins.__posthog_ykwdzsgtgp_breakpoint_handler = _breakpoint_handler


def _injected_code(bid: int):
    version_info = sys.version_info
    if version_info < (3, 11):
        return [
            Instr("LOAD_GLOBAL", "__posthog_ykwdzsgtgp_breakpoint_handler"),
            Instr("LOAD_CONST", bid),
            Instr("CALL_FUNCTION", 1),
            Instr("POP_TOP"),
        ]
    elif version_info == (3, 11):
        return [
            Instr("LOAD_GLOBAL", (True, "__posthog_ykwdzsgtgp_breakpoint_handler")),
            Instr("LOAD_CONST", bid),
            Instr("PRECALL", 1),
            Instr("CALL", 1),
            Instr("POP_TOP"),
        ]
    else:
        return [
            Instr("LOAD_GLOBAL", (True, "__posthog_ykwdzsgtgp_breakpoint_handler")),
            Instr("LOAD_CONST", bid),
            Instr("CALL", 1),
            Instr("POP_TOP"),
        ]


def _instrument_code_at_line(code, bid: int, line: int):
    last_lineno = 0
    new_instr = []
    bc = Bytecode.from_code(code)
    injected = False

    for instr in bc:
        # NOTE(Marce): This may not be enough for all cases
        if (
            hasattr(instr, "name")
            and instr.name == "LOAD_CONST"
            and inspect.iscode(instr.arg)
        ):
            # NOTE(Marce): Closures will still have the instrumentation even if we reset
            # the function.
            instr.arg = _instrument_code_at_line(instr.arg, bid, line)

        if hasattr(instr, "lineno"):
            if not injected and (
                (last_lineno != instr.lineno and last_lineno >= line)
                or (
                    instr.lineno == line
                    and (instr.name in ("RETURN_VALUE", "JUMP_BACKWARD"))
                )
            ):
                new_instr.extend(_injected_code(bid))
                injected = True
            if instr.lineno is not None:
                last_lineno = instr.lineno

        new_instr.append(instr)

    new_bc = Bytecode(new_instr)
    # We should copy more things, name, etc
    new_bc.argnames = bc.argnames
    new_bc.cellvars = bc.cellvars
    new_bc.freevars = bc.freevars
    new_bc.argcount = bc.argcount
    new_bc.filename = bc.filename
    new_bc.docstring = bc.docstring
    new_bc.posonlyargcount = bc.posonlyargcount
    new_bc.kwonlyargcount = bc.kwonlyargcount
    new_bc.flags = bc.flags
    new_bc.filename = bc.filename
    new_bc.first_lineno = bc.first_lineno
    new_bc.name = bc.name

    return new_bc.to_code()


def instrument_function_at_line(func: Callable, bid: int, lineno: int) -> bool:
    original_code = func.__code__

    if not hasattr(func, "__posthog_original_code"):
        setattr(func, "__posthog_original_code", original_code)

    func.__code__ = _instrument_code_at_line(original_code, bid, lineno)
    return True


def reset_function(func):
    if hasattr(func, "__posthog_original_code"):
        func.__code__ = getattr(func, "__posthog_original_code")


def instrument_function_at_filename_and_line(
    filename: str, lineno: int, bid: int
) -> bool:
    """
    Main entrypoint of the library, given a filename and a line number
    add a data breakpoint at that position.
    """
    target_function_obj = file_utils.find_function_at(filename, lineno)

    print(
        f"[LIVE DEBUGGER] Insturment function at {filename} {lineno}: {target_function_obj}"
    )

    if target_function_obj:
        instrument_function_at_line(target_function_obj, bid, lineno)
    else:
        return False


def reset_function_at_filename_and_line(filename: str, lineno: int) -> bool:
    target_function_obj = file_utils.find_function_at(filename, lineno)

    if target_function_obj:
        reset_function(target_function_obj)

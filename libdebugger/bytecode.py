"""
Bytecode manipulation utilities
"""

import sys
import inspect
from types import CodeType
from typing import Any, Callable, List, Optional, Tuple, Union
from bytecode import Bytecode, CompilerFlags

from bytecode.instr import Instr, Label, TryBegin, TryEnd, SetLineno

BytecodeType = Union[Instr, Label, TryBegin, TryEnd, SetLineno]
CodeGenerator = Callable[[], List[BytecodeType]]


def _is_version(ver: Tuple[int, int]) -> bool:
    version_info = sys.version_info
    major, minor = ver
    return version_info.major == major and version_info.minor == minor


def is_py39() -> bool:
    return _is_version((3, 9))


def is_py310() -> bool:
    return _is_version((3, 10))


def is_py311() -> bool:
    return _is_version((3, 11))


def is_py312() -> bool:
    return _is_version((3, 12))


def is_py313() -> bool:
    return _is_version((3, 13))


def generate_code_call_self_method(obj: Any, method_name: str) -> CodeGenerator:
    """
    Generates code to call method_name.
    Methodname must be a zero-argument method of object.
    """

    def _codegen() -> List[BytecodeType]:
        if is_py39() or is_py310():
            return [
                Instr("LOAD_CONST", obj),
                Instr("LOAD_METHOD", method_name),
                Instr("CALL_FUNCTION", 0),
                Instr("POP_TOP"),
            ]
        elif is_py311():
            return [
                Instr("LOAD_CONST", obj),  # type: ignore
                Instr("LOAD_METHOD", method_name),
                Instr("PRECALL", 0),
                Instr("CALL", 0),
                Instr("POP_TOP"),
            ]
        elif is_py312() or is_py313():
            return [
                Instr("LOAD_CONST", obj),  # type: ignore
                Instr("LOAD_ATTR", (True, method_name)),
                Instr("CALL", 0),
                Instr("POP_TOP"),
            ]
        else:
            raise RuntimeError("We don't support this version of python")

    return _codegen


def redirector_code(who_to_call: Callable[..., Any]) -> Bytecode:
    instrs: Optional[List[BytecodeType]] = None

    if is_py39() or is_py310():
        instrs = [
            Instr("LOAD_CONST", who_to_call),  # type: ignore
            Instr("LOAD_FAST", "args"),
            Instr("BUILD_MAP", 0),
            Instr("LOAD_FAST", "kwargs"),
            Instr("DICT_MERGE", 1),
            Instr("CALL_FUNCTION_EX", 1),
            Instr("RETURN_VALUE"),
        ]
    elif is_py311() or is_py312():
        instrs = [
            Instr("RESUME", 0),
            Instr("PUSH_NULL"),
            Instr("LOAD_CONST", who_to_call),  # type: ignore
            Instr("LOAD_FAST", "args"),
            Instr("BUILD_MAP", 0),
            Instr("LOAD_FAST", "kwargs"),
            Instr("DICT_MERGE", 1),
            Instr("CALL_FUNCTION_EX", 1),
            Instr("RETURN_VALUE"),
        ]
    elif is_py313():
        instrs = [
            Instr("RESUME", 0),
            Instr("LOAD_CONST", who_to_call),  # type: ignore
            Instr("PUSH_NULL"),
            Instr("LOAD_FAST", "args"),
            Instr("BUILD_MAP", 0),
            Instr("LOAD_FAST", "kwargs"),
            Instr("DICT_MERGE", 1),
            Instr("CALL_FUNCTION_EX", 1),
            Instr("RETURN_VALUE"),
        ]
    else:
        raise RuntimeError("Not compatible with this python version")

    assert instrs is not None

    bc = Bytecode(instrs)
    bc.argcount = 0
    bc.posonlyargcount = 0
    bc.kwonlyargcount = 0
    bc.argnames = ["args", "kwargs"]
    # Since we are creating a new code object we need to set some flags so the interpreter knows
    # how this works.
    bc.flags |= CompilerFlags.OPTIMIZED  # uses fast local variables
    bc.flags |= CompilerFlags.NEWLOCALS  # new local variable scope should be created
    bc.flags |= (
        CompilerFlags.VARARGS
    )  # code object accepts variable number of pos arguments
    bc.flags |= (
        CompilerFlags.VARKEYWORDS
    )  # code object accepts variable number of kw arguments

    return bc


class Injector:
    code_generator: CodeGenerator
    original_code: Optional[CodeType]

    def __init__(self, *, code_generator: CodeGenerator):
        self.code_generator = code_generator
        self.original_code = None

    def inject(self, code: CodeType) -> Bytecode:
        self.original_code = code
        bc = Bytecode.from_code(code)
        new_instrs: List[BytecodeType] = []

        prev_instr: Optional[BytecodeType] = None

        for instr in bc:
            if self.insert_now(prev_instr, instr):
                new_instrs.extend(self.code_generator())
            new_instrs.append(instr)
            prev_instr = instr

        new_bc = Bytecode(new_instrs)
        self._copy_metadata(old=bc, new=new_bc)
        return new_bc

    def insert_now(
        self, _prev_instr: Optional[BytecodeType], _instr: BytecodeType
    ) -> bool:
        return False

    def is_generator(self) -> bool:
        if self.original_code:
            return bool(self.original_code.co_flags & inspect.CO_GENERATOR)
        else:
            return False

    def _copy_metadata(self, *, old: Bytecode, new: Bytecode):
        for attr in self._get_metadata_attributes_to_copy():
            setattr(new, attr, getattr(old, attr))

    def _get_metadata_attributes_to_copy(self) -> List[str]:
        return [
            "argnames",
            "cellvars",
            "freevars",
            "argcount",
            "filename",
            "docstring",
            "posonlyargcount",
            "kwonlyargcount",
            "flags",
            "filename",
            "first_lineno",
            "name",
        ]


class EntrypointInjector(Injector):
    injected: bool

    def __init__(self, *, code_generator: CodeGenerator):
        super().__init__(code_generator=code_generator)
        self.injected = False

    def insert_now(
        self, prev_instr: Optional[BytecodeType], instr: BytecodeType
    ) -> bool:
        if self.injected:
            return False
        elif is_py310():
            if self.is_generator():
                if isinstance(prev_instr, Instr) and prev_instr.name == "GEN_START":
                    self.injected = True
                    return True
            elif prev_instr is None:
                self.injected = True
                return True
            return False
        elif is_py39() and prev_instr is None:
            self.injected = True
            return True
        elif is_py311() or is_py312() or is_py313():
            if isinstance(prev_instr, Instr) and prev_instr.name == "RESUME":
                self.injected = True
                return True
            return False
        else:
            raise RuntimeError("We don't support this version of python")

import os
import ast
import sys
from typing import Optional, Callable, List, Any


def getattrarr(obj: Any, attrarr: List[str]):
    it = obj
    rev_attrarr = [None, *reversed(attrarr)]
    while attr := rev_attrarr.pop():
        it = getattr(it, attr)
    return it


def find_module_by_path(filename):
    normalized_filename = os.path.realpath(os.path.abspath(filename))

    for name, module in sys.modules.items():
        if hasattr(module, "__file__") and module.__file__:
            module_path = os.path.realpath(os.path.abspath(module.__file__))

            if module_path == normalized_filename:
                return module

    return None


class FunctionFinder(ast.NodeVisitor):
    def __init__(self, target_lineno: int):
        self.target_lineno = target_lineno
        self.function_name = None
        self.class_stack = []

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node):
        if node.lineno <= self.target_lineno <= node.end_lineno:
            self.function_name = [*self.class_stack, node.name]


def find_function_at(filename: str, lineno: int) -> Optional[Callable]:
    """
    Given a filename and a line number, return the function object at that point.
    """
    normalized_filename = os.path.realpath(os.path.abspath(filename))
    module = find_module_by_path(filename)

    if module is None:
        return None

    with open(normalized_filename, "r") as f:
        ast_nodes = ast.parse(f.read())

    ffinder = FunctionFinder(lineno)
    ffinder.generic_visit(ast_nodes)

    if ffinder.function_name:
        return getattrarr(module, ffinder.function_name)
    else:
        return None

"""
Function discovery for instrumentation stress testing.

This module discovers instrumentable functions from the codebase,
excluding virtualenv, stdlib, and test modules.
"""

import sys
import inspect
from typing import List, Set, Callable, Optional
from pathlib import Path


def is_in_virtualenv(filepath: str) -> bool:
    """Check if a file is part of a virtual environment."""
    if not filepath:
        return True

    # Common virtualenv indicators
    venv_indicators = [
        "site-packages",
        "dist-packages",
        ".venv",
        "venv",
        "virtualenv",
        ".pyenv",
        "anaconda",
        "miniconda",
    ]

    filepath_lower = filepath.lower()
    return any(indicator in filepath_lower for indicator in venv_indicators)


def is_stdlib(filepath: str) -> bool:
    """Check if a file is part of Python's standard library."""
    if not filepath:
        return True

    try:
        # Get the stdlib path
        import sysconfig

        stdlib_path = sysconfig.get_path("stdlib")
        platstdlib_path = sysconfig.get_path("platstdlib")

        filepath_resolved = str(Path(filepath).resolve())

        if stdlib_path and filepath_resolved.startswith(
            str(Path(stdlib_path).resolve())
        ):
            return True
        if platstdlib_path and filepath_resolved.startswith(
            str(Path(platstdlib_path).resolve())
        ):
            return True

    except Exception:
        pass

    # Fallback: check if it's in a python3.x directory
    if "python3" in filepath.lower() and "lib" in filepath.lower():
        return True

    return False


def is_test_file(filepath: str) -> bool:
    """Check if a file is a test file."""
    if not filepath:
        return False

    path = Path(filepath)

    # Check if in test directory
    if "test" in path.parts or "tests" in path.parts:
        return True

    # Check if filename starts with test_
    if path.name.startswith("test_"):
        return True

    # Check if filename ends with _test.py
    if path.name.endswith("_test.py"):
        return True

    return False


def is_instrumentable_function(func: Callable) -> bool:
    """Check if a function can be safely instrumented."""
    # Skip built-in functions
    if inspect.isbuiltin(func):
        return False

    # Skip non-user functions
    if not inspect.isfunction(func) and not inspect.ismethod(func):
        return False

    # Must have __code__ attribute
    if not hasattr(func, "__code__"):
        return False

    # Skip functions that are already instrumented
    if hasattr(func, "__posthog_original_code"):
        return False

    # Skip pytest and other testing framework functions
    func_module = inspect.getmodule(func)
    if func_module:
        module_name = func_module.__name__
        if any(
            framework in module_name
            for framework in ["pytest", "_pytest", "unittest", "nose", "hypothesis"]
        ):
            return False

    return True


def discover_functions_in_module(
    module, project_name: Optional[str] = None
) -> List[Callable]:
    """Discover all instrumentable functions in a module.

    Args:
        module: The module to inspect
        project_name: If provided, only include functions whose __module__ starts with this name
    """
    functions = []

    try:
        for name, obj in inspect.getmembers(module):
            # Skip private and special methods
            if name.startswith("_"):
                continue

            if inspect.isfunction(obj):
                # If project_name is specified, only include functions from the project
                if project_name and hasattr(obj, "__module__"):
                    obj_module = obj.__module__
                    if not (
                        obj_module
                        and (
                            obj_module.startswith(f"{project_name}.")
                            or obj_module == project_name
                        )
                    ):
                        continue

                if is_instrumentable_function(obj):
                    functions.append(obj)
            elif inspect.isclass(obj):
                # Only include classes from the project
                if project_name and hasattr(obj, "__module__"):
                    obj_module = obj.__module__
                    if not (
                        obj_module
                        and (
                            obj_module.startswith(f"{project_name}.")
                            or obj_module == project_name
                        )
                    ):
                        continue

                # Discover methods in classes
                try:
                    for method_name, method in inspect.getmembers(obj):
                        if method_name.startswith("_") and method_name not in [
                            "__init__",
                            "__call__",
                        ]:
                            continue

                        if inspect.isfunction(method) or inspect.ismethod(method):
                            # Also check that the method is from the project
                            # (not inherited from stdlib base classes)
                            if project_name and hasattr(method, "__module__"):
                                method_module = method.__module__
                                if not (
                                    method_module
                                    and (
                                        method_module.startswith(f"{project_name}.")
                                        or method_module == project_name
                                    )
                                ):
                                    continue

                            if is_instrumentable_function(method):
                                functions.append(method)
                except Exception:
                    # Skip classes that can't be inspected
                    pass
    except Exception:
        # Skip modules that can't be inspected
        pass

    return functions


def get_project_root() -> Optional[Path]:
    """Find the project root by looking for pyproject.toml or setup.py."""
    cwd = Path.cwd()

    # Walk up from current directory looking for project markers
    for parent in [cwd] + list(cwd.parents):
        if (parent / "pyproject.toml").exists() or (parent / "setup.py").exists():
            return parent

    return None


def get_project_name(project_root: Optional[Path] = None) -> Optional[str]:
    """Get the project name from pyproject.toml or directory name."""
    if not project_root:
        return None

    # Try to read from pyproject.toml
    pyproject_path = project_root / "pyproject.toml"
    if pyproject_path.exists():
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                # Fallback to directory name
                return project_root.name

        try:
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
                if "project" in data and "name" in data["project"]:
                    return data["project"]["name"]
        except Exception:
            pass

    # Fallback to directory name
    return project_root.name


def is_in_project(filepath: str, project_root: Optional[Path] = None) -> bool:
    """Check if a file is part of the current project's source code."""
    if not filepath or not project_root:
        return False

    try:
        file_path = Path(filepath).resolve()
        project_path = project_root.resolve()

        # Check if file is under project directory
        if not str(file_path).startswith(str(project_path)):
            return False

        # Exclude virtualenv directories within the project
        parts = file_path.parts
        venv_indicators = {
            ".venv",
            "venv",
            ".virtualenv",
            "virtualenv",
            "env",
            ".env",
            "__pycache__",
            "site-packages",
            "dist-packages",
        }
        if any(part in venv_indicators for part in parts):
            return False

        return True
    except Exception:
        return False


def discover_all_functions(
    exclude_modules: Optional[Set[str]] = None,
) -> List[Callable]:
    """
    Discover all instrumentable functions from the current project codebase.

    Only discovers functions from modules that belong to the current project
    (module name starts with the project name).

    Excludes:
    - Test files
    - Instrumentation-related modules
    - Pytest plugin modules

    Args:
        exclude_modules: Additional module names to exclude

    Returns:
        List of instrumentable function objects
    """
    if exclude_modules is None:
        exclude_modules = set()

    # Find project root and name
    project_root = get_project_root()
    if not project_root:
        return []  # Can't find project, return empty list

    project_name = get_project_name(project_root)
    if not project_name:
        return []

    # Add instrumentation-related modules to exclusions to avoid circular dependencies
    # that cause maximum recursion errors
    exclude_modules.update(
        [
            f"{project_name}.instrumentation",  # Instrumenting the decorator causes recursion
            f"{project_name}.pytest_stress",  # Don't instrument the stress test plugin
        ]
    )

    functions = []
    seen_functions = set()  # Use id() to track unique functions

    for module_name, module in list(sys.modules.items()):
        # Skip None modules
        if module is None:
            continue

        # ONLY include modules from the current project
        if (
            not module_name
            or not module_name.startswith(f"{project_name}.")
            and module_name != project_name
        ):
            continue

        # Skip excluded modules
        if any(excluded in module_name for excluded in exclude_modules):
            continue

        # Skip modules without __file__ (built-ins)
        if not hasattr(module, "__file__") or module.__file__ is None:
            continue

        filepath = module.__file__

        # Double-check that the file is actually in the project directory (not .venv)
        if not is_in_project(filepath, project_root):
            continue

        # Skip test files
        if is_test_file(filepath):
            continue

        # Discover functions in this module (only from the project)
        module_functions = discover_functions_in_module(module, project_name)

        # Deduplicate functions (same function might appear in multiple places)
        for func in module_functions:
            func_id = id(func)
            if func_id not in seen_functions:
                seen_functions.add(func_id)
                functions.append(func)

    return functions


def get_function_info(func: Callable) -> dict:
    """Get detailed information about a function for reporting."""
    info = {
        "name": func.__name__,
        "qualname": getattr(func, "__qualname__", func.__name__),
        "module": None,
        "file": None,
        "line": None,
    }

    try:
        module = inspect.getmodule(func)
        if module:
            info["module"] = module.__name__
            if hasattr(module, "__file__"):
                info["file"] = module.__file__
    except Exception:
        pass

    try:
        if hasattr(func, "__code__"):
            info["line"] = func.__code__.co_firstlineno
    except Exception:
        pass

    return info

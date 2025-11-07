import unittest
from unittest.mock import patch

from libdebugger.instrumentation import InstrumentationDecorator
from hogtrace.vm import compile, package
from hogtrace.context import new_context, get_scope


def structural_test_function(x, y=5, *args, **kwargs):
    result = 0

    if x > 20:
        result += 100
    elif x > 10:
        result += 50
        if y > 3:
            result += 5
    else:
        result += 10
        if y < 10:
            result += 1

    counter = 0
    while counter < x:
        counter += 1
        if counter == 3:
            continue
        if counter == 7:
            break
        result += counter

    for i in range(3):
        if i > 10:
            break
        result += i
    else:
        result += 10

    for i in range(2):
        for j in range(2):
            result += i * j

    try:
        temp = x // y
        result += temp
    except ZeroDivisionError:
        result += 999
    except (ValueError, TypeError):
        result += 888
    else:
        result += 5
    finally:
        result += 1

    try:
        try:
            value = 10
            result += value
        except ValueError:
            result += 777
        finally:
            result += 2
    except ValueError:
        result += 666

    add_lambda = lambda a, b: a + b  # noqa: E731
    result += add_lambda(2, 3)

    def outer_func(n):
        count = n

        def inner_func(increment):
            nonlocal count
            count += increment
            return count

        return inner_func

    closure = outer_func(10)
    result += closure(5)  # Returns 15
    result += closure(3)  # Returns 18

    def simple_generator(limit):
        for i in range(limit):
            if i % 2 == 0:
                yield i

    for val in simple_generator(6):
        result += val  # Adds 0, 2, 4

    class SimpleContext:
        def __enter__(self):
            return 7

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    with SimpleContext() as ctx_value:
        result += ctx_value

    with SimpleContext() as a, SimpleContext() as b:
        result += a + b

    result += 100 if x > 5 else 50

    if args and args[0] > 0:
        result += args[0]

    if 0 < x <= 10:
        result += 20
    elif 10 < x <= 20:
        result += 30

    # try:
    #     match x:
    #         case 10:
    #             result += 15
    #         case 20:
    #             result += 25
    #         case _:
    #             result += 5
    # except SyntaxError:
    #     # Fallback for Python < 3.10
    #     if x == 10:
    #         result += 15
    #     elif x == 20:
    #         result += 25
    #     else:
    #         result += 5

    for arg in args:
        result += arg

    if "flag" in kwargs and kwargs["flag"]:
        result += 50

    def factorial(n):
        if n <= 1:
            return 1
        return n * factorial(n - 1)

    result += factorial(4)  # Adds 24

    def make_adder(n):
        def adder(x):
            return x + n

        return adder

    add_five = make_adder(5)
    result += add_five(3)  # Adds 8

    global _test_global
    _test_global = 100
    result += 1

    return result


def simple_function(a, b):
    c = 12 * a
    d = (b - c) / max(1, a)
    e = c * 2 + d * -1
    return e + c + d + a + b


def early_return(a, b):
    if a == 2:
        return b

    return a + b


def with_closure():
    a = 12

    def _inner():
        nonlocal a
        a += 1
        return a

    return _inner


# class TestInsturmentation(unittest.TestCase):
#     @given(
#         st.lists(st.integers(min_value=10, max_value=157)),
#         st.integers(),
#         st.integers(),
#         st.tuples(st.integers()),
#         st.booleans(),
#     )
#     @settings(deadline=None)
#     def test_instrumenting(self, linenos, arg_x, arg_y, arg_args, arg_flag):
#         expected_result = structural_test_function(
#             arg_x, arg_y, *arg_args, flag=arg_flag
#         )

#         for lineno in linenos:
#             instrument_function_at_line(structural_test_function, 0, lineno)
#         assert instrumentation_present(structural_test_function)
#         instrumented_result = structural_test_function(
#             arg_x, arg_y, *arg_args, **{"flag": arg_flag}
#         )
#         assert expected_result == instrumented_result, (
#             f"Result is not equal when instrumenting line {lineno}"
#         )
#         reset_function(structural_test_function)

#     def test_instrumenting_regression_1(self):
#         linenos = [41]
#         arg_x = 0
#         arg_y = 0
#         arg_args = (0,)
#         arg_flag = False

#         expected_result = structural_test_function(
#             arg_x, arg_y, *arg_args, flag=arg_flag
#         )

#         for lineno in linenos:
#             instrument_function_at_line(structural_test_function, 0, lineno)

#         assert instrumentation_present(structural_test_function)

#         instrumented_result = structural_test_function(
#             arg_x, arg_y, *arg_args, **{"flag": arg_flag}
#         )

#         assert expected_result == instrumented_result, (
#             f"Result is not equal when instrumenting line {lineno}"
#         )

#         reset_function(structural_test_function)

#     @given(st.lists(st.integers()))
#     @settings(deadline=None)
#     def test_reset(self, linenos):
#         original_code = structural_test_function.__code__

#         for lineno in linenos:
#             instrument_function_at_line(structural_test_function, 0, lineno)

#         if linenos:
#             assert hasattr(structural_test_function, "__posthog_original_code")
#             assert (
#                 getattr(structural_test_function, "__posthog_original_code")
#                 == original_code
#             )

#         reset_function(structural_test_function)
#         assert structural_test_function.__code__ == original_code

#     @given(a=st.integers(), b=st.integers())
#     @patch("builtins.__posthog_ykwdzsgtgp_breakpoint_handler")
#     def test_exhaustive_instrumentation(self, handler_mock, *, a, b):
#         res = simple_function(a, b)
#         expected_calls = []
#         func_first_lineno = get_lineno_of_function(simple_function)
#         for i in range(func_first_lineno + 1, func_first_lineno + 4):
#             expected_calls.append(unittest.mock.call(i))
#             instrument_function_at_line(simple_function, i, i)
#         instrumented_res = simple_function(a, b)
#         reset_function(simple_function)
#         assert instrumented_res == res
#         handler_mock.assert_has_calls(expected_calls)

#     @patch("builtins.__posthog_ykwdzsgtgp_breakpoint_handler")
#     def test_early_return(self, handler_mock):
#         lineno = get_lineno_of_function(early_return)
#         instrument_function_at_line(early_return, 0, lineno + 2)
#         early_return(2, 8)
#         handler_mock.assert_called_once()
#         reset_function(early_return)

#     @patch("builtins.__posthog_ykwdzsgtgp_breakpoint_handler")
#     def test_early_return_not_taken(self, handler_mock):
#         lineno = get_lineno_of_function(early_return)
#         instrument_function_at_line(early_return, 0, lineno + 2)
#         early_return(4, 8)
#         handler_mock.assert_not_called()
#         reset_function(early_return)

#     @patch("builtins.__posthog_ykwdzsgtgp_breakpoint_handler")
#     def test_instrumentation_in_closure(self, handler_mock):
#         lineno = get_lineno_of_function(with_closure)
#         instrument_function_at_line(with_closure, 0, lineno + 5)
#         cl = with_closure()
#         handler_mock.assert_not_called()
#         assert cl() == 13
#         handler_mock.assert_called_once()

#         reset_function(with_closure)
#         handler_mock.reset_mock()

#         cl2 = with_closure()
#         assert cl() == 14
#         assert cl2() == 13
#         # NOTE(Marce): The original closure will still keep the instrumentation
#         # until it is GC'd. New closures should not have the instrumentation
#         # anymore
#         handler_mock.assert_called_once()


# def instrumentation_present(func):
#     # TODO(Marce): Fix performance of this
#     # bc = Bytecode.from_code(func.__code__)
#     # injected_code = _injected_code(0)
#     # instrs = [i for i in bc]
#     # return contains_sublist(instrs, injected_code)
#     return True


# def get_lineno_of_function(f):
#     return f.__code__.co_firstlineno


class TestInstrumentationDecorator(unittest.TestCase):
    """Tests for the InstrumentationDecorator class lifecycle and behavior"""

    @patch("posthoganalytics.capture")
    def test_basic_capture(self, mock_capture):
        def some_function(name: str):
            return f"Hello {name}!"

        p = compile("fn:some_mod.some_func:entry { capture(name=name); }")
        pk = package("someid", p)

        InstrumentationDecorator(some_function, {(pk, p.probes[0])}, set())

        with new_context():
            scope = get_scope()
            assert scope is not None
            context_id = scope.context_id
            some_function("le_test")

        # Assert capture was called once
        mock_capture.assert_called_once()

        # Verify the call arguments
        call_args = mock_capture.call_args
        event_name = call_args[0][0]
        properties = call_args[1]["properties"]

        # Verify event name
        self.assertEqual(event_name, "$hogtrace_capture")

        # Verify all required properties
        self.assertEqual(properties["program_id"], "someid")
        self.assertEqual(properties["probe_id"], p.probes[0].id)
        self.assertEqual(properties["context_id"], context_id)

        # Verify probe_spec is present and has the correct serialized structure
        self.assertIn("probe_spec", properties)
        probe_spec = properties["probe_spec"]
        self.assertIsNotNone(probe_spec)
        # probe_spec should be a dictionary with "specifier" and "target" keys
        self.assertIsInstance(probe_spec, dict)
        self.assertEqual(probe_spec["specifier"], p.probes[0].spec.specifier)
        self.assertEqual(probe_spec["target"], p.probes[0].spec.target)

        # Verify captures
        self.assertIn("captures", properties)
        self.assertIn("name", properties["captures"])
        self.assertEqual(properties["captures"]["name"], "le_test")

        # Verify metadata properties exist
        self.assertIn("timestamp", properties)
        self.assertIn("thread_id", properties)
        self.assertIn("thread_name", properties)

    @patch("posthoganalytics.capture")
    def test_number_of_calls(self, mock_capture):
        def init():
            pass

        def some_function(name: str):
            return f"Hello {name}!"

        p = compile("""
            fn:some_mod.yes:entry { $req.count = 0; }
            fn:some_mod.some_func:entry { $req.count = $req.count + 1; capture(name=name); }
            fn:some_mod.some_func:exit { capture(count=$req.count); }
        """)
        pk = package("someid", p)

        InstrumentationDecorator(init, {(pk, p.probes[0])}, set())

        InstrumentationDecorator(
            some_function,
            {(pk, p.probes[1])},
            {(pk, p.probes[2])},
        )

        with new_context():
            scope = get_scope()
            assert scope is not None
            context_id = scope.context_id
            init()
            some_function("le_test")
            some_function("le_test")
            some_function("le_test")

        # Should be called 6 times total:
        # - 0 calls to init() (entry probe only sets $req.count, doesn't capture)
        # - 3 calls to some_function (entry probe captures name) = 3
        # - 3 calls to some_function (exit probe captures count) = 3
        # Total = 0 + 3 + 3 = 6
        self.assertEqual(mock_capture.call_count, 6)

        # Separate entry and exit calls by looking at what's captured
        entry_calls = [
            call
            for call in mock_capture.call_args_list
            if "name" in call[1]["properties"]["captures"]
        ]
        exit_calls = [
            call
            for call in mock_capture.call_args_list
            if "count" in call[1]["properties"]["captures"]
        ]

        # Verify we have the right number of each
        self.assertEqual(len(entry_calls), 3)
        self.assertEqual(len(exit_calls), 3)

        # Verify entry probes captured the name and have correct metadata
        for call in entry_calls:
            properties = call[1]["properties"]
            self.assertEqual(properties["program_id"], "someid")
            self.assertEqual(properties["probe_id"], p.probes[1].id)
            self.assertEqual(properties["context_id"], context_id)
            # Verify serialized probe_spec structure
            self.assertIsInstance(properties["probe_spec"], dict)
            self.assertEqual(
                properties["probe_spec"]["specifier"], p.probes[1].spec.specifier
            )
            self.assertEqual(
                properties["probe_spec"]["target"], p.probes[1].spec.target
            )
            self.assertEqual(properties["captures"]["name"], "le_test")

        # Verify exit probes captured count (should be 1, 2, 3) and have correct metadata
        for idx, call in enumerate(exit_calls):
            properties = call[1]["properties"]
            self.assertEqual(properties["program_id"], "someid")
            self.assertEqual(properties["probe_id"], p.probes[2].id)
            self.assertEqual(properties["context_id"], context_id)
            # Verify serialized probe_spec structure
            self.assertIsInstance(properties["probe_spec"], dict)
            self.assertEqual(
                properties["probe_spec"]["specifier"], p.probes[2].spec.specifier
            )
            self.assertEqual(
                properties["probe_spec"]["target"], p.probes[2].spec.target
            )
            self.assertEqual(properties["captures"]["count"], idx + 1)

    @patch("posthoganalytics.capture")
    def test_number_of_calls_isolation(self, mock_capture):
        def init():
            pass

        class SomeObj:
            name: str

            def __init__(self, name: str):
                self.name = name

        def some_function(obj: SomeObj):
            return f"Hello {obj.name}!"

        p = compile("""
            fn:some_mod.yes:entry { $req.count = 0; }
            fn:some_mod.some_func:entry { $req.count = $req.count + 1; capture(obj=obj); }
            fn:some_mod.some_func:exit / $req.count == 3 / { capture(count=$req.count); }
        """)
        pkg = package("someid", p)
        pkg2 = package("someid2", p)

        InstrumentationDecorator(init, {(pkg, p.probes[0]), (pkg2, p.probes[0])}, set())

        InstrumentationDecorator(
            some_function,
            {(pkg, p.probes[1]), (pkg2, p.probes[1])},
            {(pkg, p.probes[2]), (pkg2, p.probes[2])},
        )

        o1 = SomeObj("Marce")
        o2 = SomeObj("Gen")
        o3 = SomeObj("Miranda")

        context_ids = []
        with new_context():
            scope = get_scope()
            assert scope is not None
            context_ids.append(scope.context_id)
            init()
            some_function(o1)
            some_function(o2)
            some_function(o3)

        with new_context():
            scope = get_scope()
            assert scope is not None
            context_ids.append(scope.context_id)
            init()
            some_function(o1)
            some_function(o2)
            some_function(o3)

        # Each context should have:
        # - 0 calls to init() (entry probes only set $req.count, don't capture)
        # - 3 calls to some_function entry (2 probes each) = 6 per context
        # - 1 call to some_function exit when count==3 (2 probes) = 2 per context
        # Total per context = 0 + 6 + 2 = 8
        # Total for 2 contexts = 16
        self.assertEqual(mock_capture.call_count, 16)

        # Verify that obj captures contain the right names
        entry_calls = [
            call
            for call in mock_capture.call_args_list
            if "obj" in call[1]["properties"]["captures"]
        ]
        self.assertEqual(len(entry_calls), 12)  # 3 calls * 2 probes * 2 contexts

        # Verify entry probes have correct metadata
        program_ids = {"someid", "someid2"}
        for call in entry_calls:
            properties = call[1]["properties"]
            self.assertIn(properties["program_id"], program_ids)
            self.assertEqual(properties["probe_id"], p.probes[1].id)
            self.assertIn(properties["context_id"], context_ids)
            # Verify serialized probe_spec structure
            self.assertIsInstance(properties["probe_spec"], dict)
            self.assertEqual(
                properties["probe_spec"]["specifier"], p.probes[1].spec.specifier
            )
            self.assertEqual(
                properties["probe_spec"]["target"], p.probes[1].spec.target
            )
            self.assertIn("obj", properties["captures"])

        # Verify exit probes captured count=3 (should happen 4 times: 2 probes * 2 contexts)
        exit_calls = [
            call
            for call in mock_capture.call_args_list
            if "count" in call[1]["properties"]["captures"]
        ]
        self.assertEqual(len(exit_calls), 4)
        for call in exit_calls:
            properties = call[1]["properties"]
            self.assertIn(properties["program_id"], program_ids)
            self.assertEqual(properties["probe_id"], p.probes[2].id)
            self.assertIn(properties["context_id"], context_ids)
            # Verify serialized probe_spec structure
            self.assertIsInstance(properties["probe_spec"], dict)
            self.assertEqual(
                properties["probe_spec"]["specifier"], p.probes[2].spec.specifier
            )
            self.assertEqual(
                properties["probe_spec"]["target"], p.probes[2].spec.target
            )
            self.assertEqual(properties["captures"]["count"], 3)

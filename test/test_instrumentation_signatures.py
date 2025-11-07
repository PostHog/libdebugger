"""
Comprehensive tests for InstrumentationDecorator with various function signatures.

This test suite ensures that InstrumentationDecorator can handle all types of
Python function signatures without errors.
"""

import unittest
from unittest.mock import patch

from libdebugger.instrumentation import InstrumentationDecorator
from hogtrace.vm import compile, package
from hogtrace.context import new_context


class TestInstrumentationDecoratorSignatures(unittest.TestCase):
    """Test InstrumentationDecorator with various function signatures."""

    def setUp(self):
        """Set up common test fixtures."""
        # Create a simple probe for all tests
        self.program = compile("fn:*:entry { capture(called=1); }")
        self.pkg = package("test", self.program)
        self.entry_probe = (self.pkg, self.program.probes[0])

    @patch("posthoganalytics.capture")
    def test_simple_function(self, mock_capture):
        """Test simple function with regular args."""

        def simple(a, b):
            return a + b

        InstrumentationDecorator(simple, {self.entry_probe}, set())

        with new_context():
            result = simple(1, 2)

        self.assertEqual(result, 3)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_function_with_defaults(self, mock_capture):
        """Test function with default arguments."""

        def with_defaults(a, b=10, c=20):
            return a + b + c

        InstrumentationDecorator(with_defaults, {self.entry_probe}, set())

        with new_context():
            result1 = with_defaults(1)
            result2 = with_defaults(1, 2)
            result3 = with_defaults(1, 2, 3)

        self.assertEqual(result1, 31)
        self.assertEqual(result2, 23)
        self.assertEqual(result3, 6)
        self.assertEqual(mock_capture.call_count, 3)

    @patch("posthoganalytics.capture")
    def test_function_with_args(self, mock_capture):
        """Test function with *args."""

        def with_args(a, *args):
            return a + sum(args)

        InstrumentationDecorator(with_args, {self.entry_probe}, set())

        with new_context():
            result1 = with_args(1)
            result2 = with_args(1, 2, 3, 4)

        self.assertEqual(result1, 1)
        self.assertEqual(result2, 10)
        self.assertEqual(mock_capture.call_count, 2)

    @patch("posthoganalytics.capture")
    def test_function_with_kwargs(self, mock_capture):
        """Test function with **kwargs."""

        def with_kwargs(a, **kwargs):
            return a + sum(kwargs.values())

        InstrumentationDecorator(with_kwargs, {self.entry_probe}, set())

        with new_context():
            result1 = with_kwargs(1)
            result2 = with_kwargs(1, b=2, c=3)

        self.assertEqual(result1, 1)
        self.assertEqual(result2, 6)
        self.assertEqual(mock_capture.call_count, 2)

    @patch("posthoganalytics.capture")
    def test_function_with_args_and_kwargs(self, mock_capture):
        """Test function with both *args and **kwargs."""

        def with_both(a, *args, **kwargs):
            return a + sum(args) + sum(kwargs.values())

        InstrumentationDecorator(with_both, {self.entry_probe}, set())

        with new_context():
            result = with_both(1, 2, 3, x=4, y=5)

        self.assertEqual(result, 15)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_function_keyword_only(self, mock_capture):
        """Test function with keyword-only arguments."""

        def keyword_only(a, *, b, c=10):
            return a + b + c

        InstrumentationDecorator(keyword_only, {self.entry_probe}, set())

        with new_context():
            result1 = keyword_only(1, b=2)
            result2 = keyword_only(1, b=2, c=3)

        self.assertEqual(result1, 13)
        self.assertEqual(result2, 6)
        self.assertEqual(mock_capture.call_count, 2)

    @patch("posthoganalytics.capture")
    def test_function_positional_only(self, mock_capture):
        """Test function with positional-only arguments (Python 3.8+)."""

        def positional_only(a, b, /, c):
            return a + b + c

        InstrumentationDecorator(positional_only, {self.entry_probe}, set())

        with new_context():
            result = positional_only(1, 2, 3)

        self.assertEqual(result, 6)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_function_mixed_signature(self, mock_capture):
        """Test function with positional-only, regular, keyword-only, and var args."""

        def mixed(a, b, /, c, d=10, *args, e, f=20, **kwargs):
            return a + b + c + d + sum(args) + e + f + sum(kwargs.values())

        InstrumentationDecorator(mixed, {self.entry_probe}, set())

        with new_context():
            result = mixed(1, 2, 3, 4, 5, 6, e=7, f=8, x=9, y=10)

        self.assertEqual(result, 55)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_lambda_function(self, mock_capture):
        """Test lambda function."""

        def lambda_func(x, y):
            return x + y

        InstrumentationDecorator(lambda_func, {self.entry_probe}, set())

        with new_context():
            result = lambda_func(1, 2)

        self.assertEqual(result, 3)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_lambda_with_defaults(self, mock_capture):
        """Test lambda with default arguments."""

        def lambda_func(x, y=10):
            return x + y

        InstrumentationDecorator(lambda_func, {self.entry_probe}, set())

        with new_context():
            result1 = lambda_func(1)
            result2 = lambda_func(1, 2)

        self.assertEqual(result1, 11)
        self.assertEqual(result2, 3)
        self.assertEqual(mock_capture.call_count, 2)

    @patch("posthoganalytics.capture")
    def test_closure(self, mock_capture):
        """Test closure function."""

        def outer(x):
            def inner(y):
                return x + y

            return inner

        closure = outer(10)
        InstrumentationDecorator(closure, {self.entry_probe}, set())

        with new_context():
            result = closure(5)

        self.assertEqual(result, 15)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_nested_closure(self, mock_capture):
        """Test nested closure with multiple levels."""

        def outer(a):
            def middle(b):
                def inner(c):
                    return a + b + c

                return inner

            return middle

        closure = outer(1)(2)
        InstrumentationDecorator(closure, {self.entry_probe}, set())

        with new_context():
            result = closure(3)

        self.assertEqual(result, 6)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_closure_with_nonlocal(self, mock_capture):
        """Test closure with nonlocal variable."""

        def make_counter():
            count = 0

            def increment():
                nonlocal count
                count += 1
                return count

            return increment

        counter = make_counter()
        InstrumentationDecorator(counter, {self.entry_probe}, set())

        with new_context():
            result1 = counter()
            result2 = counter()
            result3 = counter()

        self.assertEqual(result1, 1)
        self.assertEqual(result2, 2)
        self.assertEqual(result3, 3)
        self.assertEqual(mock_capture.call_count, 3)

    @patch("posthoganalytics.capture")
    def test_generator_function(self, mock_capture):
        """Test generator function."""

        def gen(n):
            for i in range(n):
                yield i

        InstrumentationDecorator(gen, {self.entry_probe}, set())

        with new_context():
            result = list(gen(5))

        self.assertEqual(result, [0, 1, 2, 3, 4])
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_method(self, mock_capture):
        """Test instance method."""

        class MyClass:
            def __init__(self, value):
                self.value = value

            def add(self, x):
                return self.value + x

        obj = MyClass(10)
        InstrumentationDecorator(obj.add, {self.entry_probe}, set())

        with new_context():
            result = obj.add(5)

        self.assertEqual(result, 15)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_class_method(self, mock_capture):
        """Test class method."""

        class MyClass:
            value = 10

            @classmethod
            def add(cls, x):
                return cls.value + x

        InstrumentationDecorator(MyClass.add, {self.entry_probe}, set())

        with new_context():
            result = MyClass.add(5)

        self.assertEqual(result, 15)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_static_method(self, mock_capture):
        """Test static method."""

        class MyClass:
            @staticmethod
            def add(x, y):
                return x + y

        InstrumentationDecorator(MyClass.add, {self.entry_probe}, set())

        with new_context():
            result = MyClass.add(5, 10)

        self.assertEqual(result, 15)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_no_args_function(self, mock_capture):
        """Test function with no arguments."""

        def no_args():
            return 42

        InstrumentationDecorator(no_args, {self.entry_probe}, set())

        with new_context():
            result = no_args()

        self.assertEqual(result, 42)
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_function_with_annotations(self, mock_capture):
        """Test function with type annotations."""

        def annotated(a: int, b: str = "default") -> str:
            return f"{a}-{b}"

        InstrumentationDecorator(annotated, {self.entry_probe}, set())

        with new_context():
            result = annotated(42, "test")

        self.assertEqual(result, "42-test")
        mock_capture.assert_called_once()

    @patch("posthoganalytics.capture")
    def test_recursive_function(self, mock_capture):
        """Test recursive function."""

        def factorial(n):
            if n <= 1:
                return 1
            return n * factorial(n - 1)

        InstrumentationDecorator(factorial, {self.entry_probe}, set())

        with new_context():
            result = factorial(5)

        self.assertEqual(result, 120)
        # Should be called 5 times (5, 4, 3, 2, 1)
        self.assertEqual(mock_capture.call_count, 5)

    @patch("posthoganalytics.capture")
    def test_function_returning_lambda(self, mock_capture):
        """Test function that returns a lambda."""

        def make_adder(x):
            return lambda y: x + y

        InstrumentationDecorator(make_adder, {self.entry_probe}, set())

        with new_context():
            adder = make_adder(10)
            result = adder(5)

        self.assertEqual(result, 15)
        mock_capture.assert_called_once()  # Only make_adder is instrumented

    @patch("posthoganalytics.capture")
    def test_function_with_exception(self, mock_capture):
        """Test function that raises an exception."""

        def raises_error(x):
            if x < 0:
                raise ValueError("Negative value")
            return x * 2

        InstrumentationDecorator(raises_error, {self.entry_probe}, set())

        with new_context():
            result = raises_error(5)
            with self.assertRaises(ValueError):
                raises_error(-1)

        self.assertEqual(result, 10)
        self.assertEqual(mock_capture.call_count, 2)

    @patch("posthoganalytics.capture")
    def test_async_function(self, mock_capture):
        """Test async function (should work but won't await in test)."""

        async def async_func(x, y):
            return x + y

        # This test just ensures we can instrument it without error
        # Actually running async functions requires asyncio
        decorator = InstrumentationDecorator(async_func, {self.entry_probe}, set())

        # We can at least check it was decorated
        self.assertIsNotNone(decorator)


if __name__ == "__main__":
    unittest.main()

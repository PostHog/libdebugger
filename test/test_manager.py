import unittest
from unittest.mock import MagicMock, patch
from typing import List

from collections import Counter
from hypothesis import given, strategies as st, settings
from bytecode import Bytecode
from libdebugger import Breakpoint
from libdebugger.manager import LiveDebuggerManager
from libdebugger.instrumentation import reset_function
from libdebugger.file_utils import find_function_at


def function_a(n):
    a = 12
    b = 14
    return a + b + n


def function_b(n):
    a = 12
    b = 14
    return a + b + n


def function_c(n):
    a = 12
    b = 14
    return a + b + n


def function_d(n):
    a = 12
    b = 14
    return a + b + n


def function_e(n):
    a = 12
    b = 14
    return a + b + n


FUNCTION_POOL = [
    function_a,
    function_b,
    function_c,
    function_d,
    function_e,
]


def get_lineno_of_function(f):
    return f.__code__.co_firstlineno


def function_is_instrumented(f):
    bc = Bytecode.from_code(f.__code__)

    for instr in bc:
        if instr.name == "LOAD_GLOBAL":
            if instr.arg == (True, "__posthog_ykwdzsgtgp_breakpoint_handler"):
                return True
            elif instr.arg == "__posthog_ykwdzsgtgp_breakpoint_handler":
                return True
    return False


@st.composite
def breakpoint_strategy(draw):
    uuid = draw(st.uuids())
    function = draw(st.sampled_from(FUNCTION_POOL))
    line_offset = draw(st.integers(min_value=1, max_value=3))

    return Breakpoint(
        uuid=uuid,
        filename=__file__,
        lineno=get_lineno_of_function(function) + line_offset,
        conditional_expr=None,
    )


class TestManager(unittest.TestCase):
    @given(
        breakpoints_over_time=st.lists(
            st.lists(breakpoint_strategy(), max_size=5), max_size=5
        )
    )
    @settings(deadline=None)
    @patch("libdebugger.instrumentation._enqueue_message")
    def test_update_breakpoints(
        self, enqueue_mock, *, breakpoints_over_time: List[List[Breakpoint]]
    ):
        """
        == Property 1 ==

        Let I(t) denote the set of instrumented functions at time t,
        and let U(t) be the most recent update operation before time t.

        Then,

        ∀t: I(t) = functions_specified_in(U(t))

        In plain terms: The set of instrumented functions in the system must
        be exactly equal to the set of functions that were specified in the
        last breakpoint update.

        == Property 2 ==

        The number of executions of _enqueue_message for a function where N
        breakpoints exist for a particular line is N.

        """
        for function in FUNCTION_POOL:
            reset_function(function)

        mock_client = MagicMock()
        bm = LiveDebuggerManager(mock_client)

        for breakpoints_now in breakpoints_over_time:
            bm._update_breakpoints(breakpoints_now)

            # After an update breakpoints call we should only have instrumented the functions
            # from the last breakpoints
            func_pool_set = set(FUNCTION_POOL)
            expected_instrumented_func_set = set()

            expected_enqueue_calls_per_func = Counter()

            for bp in breakpoints_now:
                fun = find_function_at(bp.filename, bp.lineno)
                expected_enqueue_calls_per_func[fun] += 1
                expected_instrumented_func_set.add(fun)

            expected_non_instrumented_func_set = (
                func_pool_set - expected_instrumented_func_set
            )

            instrumented_func_set = {
                f for f in FUNCTION_POOL if function_is_instrumented(f)
            }
            non_instrumented_func_set = {
                f for f in FUNCTION_POOL if not function_is_instrumented(f)
            }

            assert func_pool_set == instrumented_func_set.union(
                non_instrumented_func_set
            )
            assert not instrumented_func_set.intersection(non_instrumented_func_set)

            # Main checks for property #1
            assert instrumented_func_set == expected_instrumented_func_set
            assert non_instrumented_func_set == expected_non_instrumented_func_set

            # Main checks for property #2
            for func in instrumented_func_set:
                enqueue_mock.reset_mock()
                assert func(2) == 28
                enqueue_mock.assert_called()
                assert expected_enqueue_calls_per_func[func] == enqueue_mock.call_count

            for func in non_instrumented_func_set:
                enqueue_mock.reset_mock()
                assert func(2) == 28
                enqueue_mock.assert_not_called()

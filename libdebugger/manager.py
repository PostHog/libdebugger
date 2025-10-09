import logging
from typing import List, Optional, Tuple
from posthoganalytics.poller import Poller
from datetime import timedelta
from posthoganalytics.request import get, APIError
from collections import defaultdict

from libdebugger import Breakpoint
from libdebugger.instrumentation import (
    register_breakpoints,
    reset_breakpoint_registry,
    instrument_function_at_filename_and_line,
    reset_function_at_filename_and_line,
)


class LiveDebuggerManager:
    def __init__(self, client, poll_interval: int = 30):
        self.client = client
        self.poll_interval = poll_interval
        self.log = logging.getLogger("posthog.data_breakpoints")

        self.breakpoints: List[Breakpoint] = []
        self.poller: Optional[Poller] = None
        self.enabled = False
        self.bid_counter = 1

        self.filepos_to_bids = {}

    def start(self):
        if self.enabled:
            self.log_info("Already started")
            return

        if self.client.personal_api_key:
            self.poller = Poller(
                interval=timedelta(seconds=self.poll_interval),
                execute=self._fetch_breakpoints,
            )
            self.poller.start()
            self.enabled = True

    def stop(self):
        self.poller.stop()

        for bp in self.breakpoints:
            reset_function_at_filename_and_line(bp.filename, bp.lineno)

        self.enabled = False

    def _fetch_breakpoints(self):
        if not self.client.personal_api_key:
            return

        try:
            response = get(
                self.client.personal_api_key,
                f"/api/environments/@current/live_debugger_breakpoints/?token={self.client.api_key}",
                self.client.host,
                timeout=10,
            )

            new_breakpoints = []

            for bp_data in response.get("results", []):
                bp = Breakpoint(
                    uuid=bp_data["id"],
                    filename=bp_data["filename"],
                    lineno=bp_data["line_number"],
                    conditional_expr=bp_data.get("condition"),
                )
                new_breakpoints.append(bp)

            self._update_breakpoints(new_breakpoints)
        except APIError as e:
            self.log_warning(f"Error fetching breakpoints: {e.status} - {e}")
        except Exception as e:
            self.log_warning(f"Error fetching breakpoints: {e}")

    def _update_breakpoints(self, latest_breakpoints: List[Breakpoint]):
        """
        Update the functions in the system based on the latest breakpoints.

        First we find out which functions need to be instrumented, and which
        breakpoints they correspond to. Many breakpoints can refer to the same
        piece of code so we deduplicate and create a mapping of filename, lineno
        to breakpoint uuids.

        This is a simple baseline implementation, this can be optimized in the
        future, for now this logic is as simple and understandable as possible.

        TODO(Marce): Need to evaluate what happens if the function crashes at any point
        """
        old_set = {(bp.uuid, bp.filename, bp.lineno) for bp in self.breakpoints}
        new_set = {(bp.uuid, bp.filename, bp.lineno) for bp in latest_breakpoints}

        files_to_instrument = defaultdict(list)

        # Deduplicate by filepos (filename, lineno) and register the new breakpoints
        for bp in latest_breakpoints:
            files_to_instrument[(bp.filename, bp.lineno)].append(bp)

        breakpoints_to_remove = old_set - new_set
        new_breakpoints = new_set - old_set

        if not breakpoints_to_remove and not new_breakpoints:
            return

        # NOTE(Marce): Not being clever on purpose for now, reset everything and
        # regenerate.
        reset_breakpoint_registry()

        for uuid, filename, lineno in breakpoints_to_remove:
            reset_function_at_filename_and_line(filename, lineno)

        for filename, lineno in files_to_instrument.keys():
            reset_function_at_filename_and_line(filename, lineno)

        for (filename, lineno), bps in files_to_instrument.items():
            bid = self.get_bid_by_filepos((filename, lineno))

            instrument_function_at_filename_and_line(filename, lineno, bid)

            register_breakpoints(bid, bps)

        self.breakpoints = latest_breakpoints

    def get_bid_by_filepos(self, filepos: Tuple[str, int]) -> int:
        bid = self.filepos_to_bids.get(filepos)

        if bid is None:
            bid = self.bid_counter
            self.filepos_to_bids[filepos] = bid
            self.bid_counter += 1

        return bid

    def log_info(self, message: str):
        self.log.info(self._format_message(message))

    def log_warning(self, message: str):
        self.log.warning(self._format_message(message))

    def _format_message(self, message: str) -> str:
        return f"[LIVE DEBUGGER] {message}"

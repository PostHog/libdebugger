from typing import Dict, Optional
from posthoganalytics.poller import Poller
from datetime import timedelta
from posthoganalytics import Posthog

from libdebugger.program import ProgramManager


class HogTraceManager:
    client: Posthog
    poll_interval: int

    programs: Dict[str, ProgramManager]
    enabled: bool
    poller: Optional[Poller]

    def __init__(self, client: Posthog, poll_interval: int = 30):
        self.client = client
        self.poll_interval = poll_interval

        self.programs = {}
        self.enabled = False
        self.poller = None

    def start(self):
        if self.enabled:
            self.log_info("HogTraceManager already started")
            return

        if self.client.personal_api_key:
            self.poller = Poller(
                interval=timedelta(seconds=self.poll_interval),
                execute=self._fetch_programs,
            )
            self.poller.start()
            self.enabled = True

    def stop(self):
        if self.poller:
            self.poller.stop()

        for program in self.programs.values():
            program.uninstall()

        self.enabled = False

    def _fetch_programs(self):
        """
        Fetch the latest active programs and install, uninstall or reinstall them.
        """
        if not self.client.personal_api_key:
            self.log_warning("No API key")
            return

        try:
            pass
            # response = get(
            #     self.client.personal_api_key,
            #     "/api/projects/@current/live_debugger/programs/active",
            #     self.client.host,
            #     timeout=10,
            # )

            # programs = Programs.from_bytes(response)

            # existing_ids: Set[str] = set(self.programs.keys())
            # new_ids: Set[str] = set(programs.keys())

            # ids_to_uninstall: Set[str] = existing_ids - new_ids

            # for id_ in ids_to_uninstall:
            #     if existing_program := self.programs.get(id_):
            #         del self.programs[id_]
            #         existing_program.uninstall()

            # for id_, new_program in programs.items():
            #     if existing_program := self.programs.get(id_):
            #         existing_program.update(new_program)
            #     else:
            #         self.programs[id_] = ProgramManager(id_, new_program)

            # assert set(self.programs.keys()) == new_ids

        except RuntimeError:
            pass

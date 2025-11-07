from typing import Optional, Set

from hogtrace import Program, ProbeSpec


class Probe:
    pass


class ProgramManager:
    """
    Manager for hogtrace programs.
    """

    id_: str
    installed_hash: Optional[int]
    program: Program
    installed_probes: Set[ProbeSpec]

    def __init__(self, id_: str, program: Program):
        self.id_ = id_
        self.installed_hash = None
        self.program = program
        self.installed_probes = set()

    def update(self, program: Program):
        """
        If there has been an update (hashes differ),
        reinstall this program. Else, do nothing.
        """
        if program.hash != self.installed_hash:
            self._update_from_program(program)
            self.reinstall()

    def reinstall(self):
        pass

    def install(self):
        """
        Installs this program in the system.
        """
        # for probe in self.program.probes:
        #     install_probe(probe)
        pass

    def uninstall(self):
        pass

    def _update_from_program(self, program: Program):
        self.version = program.version
        self.program = program

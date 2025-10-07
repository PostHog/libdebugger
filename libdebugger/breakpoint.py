from dataclasses import dataclass
from typing import Optional


@dataclass
class Breakpoint:
    uuid: str
    filename: str
    lineno: int
    conditional_expr: Optional[str]

    def condition_matches(self, locs):
        if self.conditional_expr:
            return False
        else:
            return True

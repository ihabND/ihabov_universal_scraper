"""Plug-in point for captcha and bot-check solvers (2Captcha, CapSolver, ...).

A browser fetcher that sees a bot check runs every solver registered for that vendor (a key of
detect.CHALLENGES), then keeps waiting for the check to clear.
"""

from typing import Any, Protocol


class Solver(Protocol):
    vendors: set[str]

    async def solve(self, tab: Any, vendor: str) -> None: ...  # tab is a Playwright Page


SOLVERS: list[Solver] = []


async def run_solvers(tab: Any, vendor: str) -> None:
    for solver in SOLVERS:
        if vendor in solver.vendors:
            await solver.solve(tab, vendor)

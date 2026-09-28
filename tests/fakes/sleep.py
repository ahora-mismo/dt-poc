"""Recording `Sleeper` fake (D-52): never actually sleeps."""

from dt_agent.ports.sleep import Sleeper


class FakeSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# Structural check, not a test: see tests/fakes/mail.py's `_mail_port_check`.
_sleeper_check: Sleeper = FakeSleeper()

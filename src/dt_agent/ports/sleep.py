"""Injectable sleep (D-20, D-30, D-43).

Production passes `anyio.sleep`; tests pass `tests/fakes/sleep.py`'s
`FakeSleeper`, which records requested delays instead of waiting (per the
testing rules: "anything that really sleeps" is never used in a test).
"""

from collections.abc import Awaitable, Callable

type Sleeper = Callable[[float], Awaitable[None]]

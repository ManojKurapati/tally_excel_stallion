"""Exponential backoff retry helper."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

DEFAULT_BACKOFF: tuple[float, ...] = (2, 5, 15, 30, 60)


class RetryExhausted(Exception):
    """Raised when all retry attempts failed."""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"operation failed after {attempts} attempt(s): {last_error}")
        self.attempts = attempts
        self.last_error = last_error


def backoff_delay(attempt: int, schedule: Sequence[float] = DEFAULT_BACKOFF) -> float:
    """Delay before retrying after failed attempt number `attempt` (1-based)."""
    if not schedule:
        return 0.0
    index = min(max(attempt, 1), len(schedule)) - 1
    return float(schedule[index])


def retry[T](
    operation: Callable[[], T],
    *,
    attempts: int = 5,
    schedule: Sequence[float] = DEFAULT_BACKOFF,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    non_retryable: tuple[type[BaseException], ...] = (),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `operation`, retrying with exponential backoff.

    `non_retryable` exceptions are re-raised immediately (e.g. validation or
    authentication errors). Any other exception matching `retry_on` is retried
    until `attempts` is exhausted, after which `RetryExhausted` is raised with
    the last error attached.
    """
    attempts = max(1, attempts)
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except non_retryable:
            raise
        except retry_on as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = backoff_delay(attempt, schedule)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
    assert last_error is not None
    raise RetryExhausted(attempts, last_error) from last_error

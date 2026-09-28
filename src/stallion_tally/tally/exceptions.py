"""Tally specific exceptions."""

from __future__ import annotations


class TallyError(Exception):
    """Base class for all Tally errors."""

    retryable: bool = False


class TallyUnavailableError(TallyError):
    """Tally is not running / not reachable on the configured port."""

    retryable = True


class TallyTimeoutError(TallyError):
    """Tally did not answer within the configured timeout."""

    retryable = True


class TallyRequestError(TallyError):
    """Tally answered with an unexpected HTTP status."""

    retryable = True


class TallyResponseError(TallyError):
    """Tally answered with an application level error (LINEERROR etc)."""

    retryable = False


class TallyParseError(TallyError):
    """The response could not be parsed as Tally XML."""

    retryable = False

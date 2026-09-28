"""HTTP client for TallyPrime's XML-over-HTTP interface.

All communication with Tally goes through `TallyClient`. Responses can be
streamed straight to disk (raw preservation + bounded memory) and are checked
for Tally application errors before they are handed to the parsers.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

from stallion_tally.logging import get_logger
from stallion_tally.tally import xml_builder
from stallion_tally.tally.exceptions import (
    TallyError,
    TallyRequestError,
    TallyTimeoutError,
    TallyUnavailableError,
)
from stallion_tally.tally.xml_parser import decode_tally_bytes, raise_for_tally_error
from stallion_tally.utils.retry import DEFAULT_BACKOFF, RetryExhausted, retry

log = get_logger(__name__)

_RETRYABLE = (TallyUnavailableError, TallyTimeoutError, TallyRequestError)


@dataclass(frozen=True)
class TallyResponse:
    """A raw response from Tally, either in memory or saved to a file."""

    request_name: str
    source: bytes | Path
    size_bytes: int
    elapsed_seconds: float

    @property
    def path(self) -> Path | None:
        return self.source if isinstance(self.source, Path) else None

    def raw_bytes(self) -> bytes:
        return self.source if isinstance(self.source, bytes) else self.source.read_bytes()


class TallyClient:
    """Thin, reusable client for the local TallyPrime server."""

    def __init__(
        self,
        base_url: str = "http://localhost:9000",
        *,
        timeout: float = 300.0,
        connect_timeout: float = 5.0,
        retry_attempts: int = 3,
        backoff_schedule: Sequence[float] = DEFAULT_BACKOFF,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._retry_attempts = max(1, retry_attempts)
        self._backoff = tuple(backoff_schedule)
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            transport=transport,
            headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml"},
        )

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TallyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ availability
    def server_info(self) -> str:
        """Text Tally returns for a plain GET (e.g. 'TallyPrime Server is Running')."""
        try:
            response = self._client.get("/")
        except httpx.ConnectError as exc:
            raise TallyUnavailableError(
                f"Tally is not reachable at {self.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise TallyTimeoutError(f"Tally did not answer at {self.base_url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TallyRequestError(f"request to Tally failed: {exc}") from exc
        if response.status_code != 200:
            raise TallyRequestError(f"Tally answered HTTP {response.status_code}")
        return decode_tally_bytes(response.content).strip()

    def is_available(self) -> bool:
        try:
            self.server_info()
        except TallyError:
            return False
        return True

    # ------------------------------------------------------------------ transport
    def send(self, request_xml: bytes, *, name: str, save_to: Path | None = None) -> TallyResponse:
        """POST a request to Tally with retries on transport failures.

        When `save_to` is given the response body is streamed to that file and
        the file path is returned as the response source.
        """

        def on_retry(attempt: int, error: BaseException, delay: float) -> None:
            log.warning(
                "Tally request failed, retrying",
                request=name,
                attempt=attempt,
                retry_in_seconds=delay,
                error=str(error),
            )

        try:
            return retry(
                lambda: self._send_once(request_xml, name=name, save_to=save_to),
                attempts=self._retry_attempts,
                schedule=self._backoff,
                retry_on=_RETRYABLE,
                on_retry=on_retry,
                sleep=self._sleep,
            )
        except RetryExhausted as exc:
            raise exc.last_error from exc

    def _send_once(self, request_xml: bytes, *, name: str, save_to: Path | None) -> TallyResponse:
        started = time.monotonic()
        try:
            if save_to is None:
                response = self._client.post("/", content=request_xml)
                self._check_status(response)
                content = response.content
                size = len(content)
                source: bytes | Path = content
            else:
                save_to.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = save_to.with_suffix(save_to.suffix + ".part")
                size = 0
                with self._client.stream("POST", "/", content=request_xml) as response:
                    self._check_status(response)
                    with tmp_path.open("wb") as fh:
                        for chunk in response.iter_bytes():
                            fh.write(chunk)
                            size += len(chunk)
                os.replace(tmp_path, save_to)
                source = save_to
        except httpx.ConnectError as exc:
            raise TallyUnavailableError(
                f"Tally is not reachable at {self.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise TallyTimeoutError(f"Tally request {name!r} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TallyRequestError(f"Tally request {name!r} failed: {exc}") from exc

        elapsed = time.monotonic() - started
        log.debug(
            "Tally response received", request=name, size_bytes=size, seconds=round(elapsed, 2)
        )
        raise_for_tally_error(source)
        return TallyResponse(
            request_name=name, source=source, size_bytes=size, elapsed_seconds=elapsed
        )

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise TallyRequestError(f"Tally answered HTTP {response.status_code}")

    # ------------------------------------------------------------------ datasets
    def get_companies(self, save_to: Path | None = None) -> TallyResponse:
        return self.send(xml_builder.build_companies_request(), name="companies", save_to=save_to)

    def get_masters(self, company: str, dataset: str, save_to: Path | None = None) -> TallyResponse:
        return self.send(
            xml_builder.build_masters_request(company, dataset), name=dataset, save_to=save_to
        )

    def get_groups(self, company: str, save_to: Path | None = None) -> TallyResponse:
        return self.get_masters(company, "groups", save_to)

    def get_ledgers(self, company: str, save_to: Path | None = None) -> TallyResponse:
        return self.get_masters(company, "ledgers", save_to)

    def get_stock_items(self, company: str, save_to: Path | None = None) -> TallyResponse:
        return self.get_masters(company, "stock_items", save_to)

    def get_bills(self, company: str, save_to: Path | None = None) -> TallyResponse:
        """Outstanding bills via the `Bills` TDL collection."""
        return self.send(
            xml_builder.build_bills_collection_request(company), name="bills", save_to=save_to
        )

    def get_bills_report(
        self, company: str, receivable: bool, save_to: Path | None = None
    ) -> TallyResponse:
        """Outstanding bills via the built-in Bills Receivable/Payable reports."""
        name = "bills_receivable" if receivable else "bills_payable"
        return self.send(
            xml_builder.build_bills_report_request(company, receivable), name=name, save_to=save_to
        )

    def get_day_book(
        self, company: str, from_date: date, to_date: date, save_to: Path | None = None
    ) -> TallyResponse:
        return self.send(
            xml_builder.build_day_book_request(company, from_date, to_date),
            name="vouchers",
            save_to=save_to,
        )

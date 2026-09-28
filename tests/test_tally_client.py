from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from stallion_tally.models import Company
from stallion_tally.tally.client import TallyClient
from stallion_tally.tally.exceptions import (
    TallyResponseError,
    TallyTimeoutError,
    TallyUnavailableError,
)
from stallion_tally.tally.xml_parser import parse_companies, parse_vouchers
from stallion_tally.utils.retry import RetryExhausted, backoff_delay, retry
from tests.conftest import COMPANY_1, COMPANY_1_ID, FakeTally


def test_is_available(tally_client: TallyClient, fake_tally: FakeTally) -> None:
    assert tally_client.is_available() is True
    assert "Running" in tally_client.server_info()
    fake_tally.available = False
    assert tally_client.is_available() is False
    with pytest.raises(TallyUnavailableError):
        tally_client.server_info()


def test_get_companies(tally_client: TallyClient) -> None:
    response = tally_client.get_companies()
    companies = parse_companies(response.raw_bytes())
    assert [c.name for c in companies][0] == COMPANY_1
    assert response.request_name == "companies"


def test_retries_transport_errors_then_succeeds(
    tally_client: TallyClient, fake_tally: FakeTally
) -> None:
    fake_tally.fail_next_requests = 2
    response = tally_client.get_companies()
    assert len(parse_companies(response.raw_bytes())) == 2


def test_gives_up_after_configured_attempts(fake_tally: FakeTally) -> None:
    fake_tally.fail_next_requests = 5
    client = TallyClient(
        retry_attempts=2,
        backoff_schedule=(0,),
        transport=fake_tally.transport(),
        sleep=lambda _s: None,
    )
    with pytest.raises(TallyUnavailableError):
        client.get_companies()
    assert fake_tally.fail_next_requests == 3


def test_timeout_is_reported(tally_client: TallyClient, fake_tally: FakeTally) -> None:
    fake_tally.timeout_next_requests = 5
    with pytest.raises(TallyTimeoutError):
        tally_client.get_companies()


def test_tally_errors_are_not_retried(tally_client: TallyClient, fake_tally: FakeTally) -> None:
    with pytest.raises(TallyResponseError, match="not loaded"):
        tally_client.get_ledgers("Nope Ltd")
    assert len(fake_tally.requests) == 1


def test_stream_to_file_and_parse(tally_client: TallyClient, tmp_path: Path) -> None:
    target = tmp_path / "raw" / "vouchers_001.xml"
    response = tally_client.get_day_book(
        COMPANY_1, date(2026, 9, 1), date(2026, 9, 30), save_to=target
    )
    assert response.path == target and target.exists()
    assert not target.with_suffix(".xml.part").exists()
    company = Company(name=COMPANY_1, tally_guid=COMPANY_1_ID)
    vouchers = list(parse_vouchers(target, company))
    assert {v.voucher_number for v in vouchers} == {"SI/2026/0091", "PAY/2026/0007"}


def test_http_error_status_is_retryable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = TallyClient(retry_attempts=1, transport=httpx.MockTransport(handler))
    with pytest.raises(Exception, match="HTTP 500"):
        client.get_companies()


def test_retry_helper_backoff_and_exhaustion() -> None:
    delays: list[float] = []
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        raise ValueError("nope")

    with pytest.raises(RetryExhausted) as info:
        retry(flaky, attempts=3, schedule=(1, 2, 3), sleep=delays.append)
    assert calls["n"] == 3 and delays == [1, 2]
    assert isinstance(info.value.last_error, ValueError)
    assert backoff_delay(10, (2, 5, 15, 30, 60)) == 60


def test_retry_helper_respects_non_retryable() -> None:
    calls = {"n": 0}

    def bad() -> None:
        calls["n"] += 1
        raise KeyError("permanent")

    with pytest.raises(KeyError):
        retry(bad, attempts=5, schedule=(0,), non_retryable=(KeyError,), sleep=lambda _s: None)
    assert calls["n"] == 1

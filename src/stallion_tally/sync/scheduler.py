"""Background agent loop: wait for Tally, sync on an interval, back off when Tally is down."""

from __future__ import annotations

import signal
import threading
from typing import Any

from stallion_tally.config import Settings
from stallion_tally.logging import get_logger
from stallion_tally.sync.manager import SyncManager, SyncRunResult
from stallion_tally.tally.client import TallyClient

log = get_logger(__name__)


class Agent:
    def __init__(
        self,
        settings: Settings,
        manager: SyncManager,
        client: TallyClient,
        *,
        stop_event: threading.Event | None = None,
        install_signal_handlers: bool = True,
    ) -> None:
        self.settings = settings
        self.manager = manager
        self.client = client
        self.stop_event = stop_event or threading.Event()
        self._install_signal_handlers = install_signal_handlers
        self.last_result: SyncRunResult | None = None

    def stop(self, *_: Any) -> None:
        log.info("Stop requested")
        self.stop_event.set()

    def _install_signals(self) -> None:
        if not self._install_signal_handlers:
            return
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):  # not in main thread
                pass

    def _wait(self, seconds: float) -> bool:
        """Sleep unless stopped; returns True when a stop was requested."""
        return self.stop_event.wait(seconds)

    def run_forever(
        self, once: bool = False, max_cycles: int | None = None
    ) -> SyncRunResult | None:
        self._install_signals()
        backoff = self.settings.tally_unavailable_backoff_seconds
        cycles = 0
        log.info(
            "Agent started",
            interval_seconds=self.settings.sync_interval_seconds,
            tally=self.client.base_url,
        )
        while not self.stop_event.is_set():
            if not self.client.is_available():
                log.warning(
                    "Tally not available, waiting",
                    retry_in_seconds=backoff,
                    url=self.client.base_url,
                )
                if self._wait(backoff):
                    break
                backoff = min(backoff * 2, self.settings.tally_unavailable_max_backoff_seconds)
                continue
            backoff = self.settings.tally_unavailable_backoff_seconds

            try:
                self.last_result = self.manager.run(trigger="agent")
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.error("Sync cycle crashed", error=str(exc), exc_info=True)
            cycles += 1
            if once or (max_cycles is not None and cycles >= max_cycles):
                break
            if self._wait(self.settings.sync_interval_seconds):
                break
        log.info("Agent stopped", cycles=cycles)
        return self.last_result


def run_agent(
    settings: Settings, manager: SyncManager, client: TallyClient, once: bool = False
) -> SyncRunResult | None:
    return Agent(settings, manager, client).run_forever(once=once)

"""Send eligible reminders from the running desktop service."""
from collections.abc import Callable
from datetime import date, datetime
import logging
from pathlib import Path
from threading import Event, Thread

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .mobile_pairing import DesktopPairing
from .reminder_delivery import reserve_push, settle_push
from .web_push import WebPushSender


LOG = logging.getLogger(__name__)
POLL_SECONDS = 5
RETRY_SECONDS = 30


class MobilePush:
    def __init__(self, pairing: DesktopPairing, engine: Engine, folder: Path,
                 day: Callable[[Session], date], now: Callable[[], datetime], *, sender=None):
        self.pairing = pairing
        self.engine = engine
        self.folder = folder
        self.day = day
        self.now = now
        self.sender = sender if sender is not None else WebPushSender()
        self.stopped = Event()
        self.thread = Thread(target=self.run, name="mobile-push", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=8)
        self.sender.close()

    def run(self) -> None:
        delay = POLL_SECONDS
        while not self.stopped.wait(delay):
            try:
                delay = self.tick()
            except Exception as error:
                LOG.warning("Mobile push could not finish (%s)", type(error).__name__)
                delay = RETRY_SECONDS

    def tick(self) -> int:
        runtime = self.pairing.gate()
        if runtime is not None and not runtime.enter():
            return POLL_SECONDS
        try:
            target = self.pairing.current_push()
            if target is None:
                return POLL_SECONDS
            notice = reserve_push(self.engine, self.folder, self.day, self.now(), target.receiver)
            if notice is None:
                return POLL_SECONDS
            current = self.pairing.current_push()
            ttl = min(45, int((notice.expires_at - self.now()).total_seconds()) - 5)
            if ttl < 1 or current != target:
                settle_push(self.engine, notice, delivered=False)
                return POLL_SECONDS
            result = self.sender.send(target, {
                "v": 1, "delivery": notice.id, "workspace": notice.workspace, "lessonId": notice.lesson_id,
                "pairing": target.pairing,
            }, ttl)
            # A provider can take delivery even when its response did not reach this process.
            with self.pairing.hold_push_target(target) as target_is_current:
                delivered = target_is_current and result in ("accepted", "uncertain")
                settle_push(self.engine, notice, delivered=delivered)
            if result == "expired":
                self.pairing.expire_push(target)
            return POLL_SECONDS if delivered or not target_is_current else RETRY_SECONDS
        finally:
            if runtime is not None:
                runtime.leave()

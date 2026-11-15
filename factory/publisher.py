import argparse
import os
from typing import Protocol
import httpx
from datetime import timedelta
from time import sleep

from sqlalchemy import select, update

from factory.db import configured_ledger, now, targets
from factory.storage import verified


class Publisher(Protocol):
    provider: str

    def dispatch(self, ledger) -> bool: ...


class MockPublisher:
    provider = "mock"

    def dispatch(self, ledger):
        return dispatch_mock(ledger, self)

    @staticmethod
    def receipt(key: str) -> str:
        return "mock-" + key[:32]

    def reconcile(self, key: str, post_id: str) -> dict:
        if post_id != self.receipt(key):
            raise ValueError("Unexpected mock receipt")
        return {
            "provenance": "mock_analytics",
            "views": int(key[:4], 16) % 250,
            "captured_at": now().isoformat(),
        }


def dispatch(ledger, adapter: Publisher):
    return adapter.dispatch(ledger)


def dispatch_mock(ledger, adapter):
    at = now()
    with ledger.write() as db:
        query = (
            select(targets)
            .where(
                targets.c.due <= at.isoformat(),
                targets.c.snapshot["provider"].as_string().is_distinct_from("youtube"),
                targets.c.state.in_(("SCHEDULED", "RECONCILIATION_REQUIRED")),
            )
            .order_by(targets.c.due)
            .limit(1)
        )
        row = db.execute(ledger.lock(query, skip=True)).mappings().first()
        if row is None:
            return False
        values = {}
        try:
            verified(row.snapshot["sha256"])
            if row.state == "SCHEDULED":
                values = {
                    "state": "RECONCILIATION_REQUIRED",
                    "post_id": adapter.receipt(row.id),
                    "due": (at + timedelta(seconds=1)).isoformat(),
                }
            else:
                values = {
                    "state": "PUBLISHED",
                    "analytics": adapter.reconcile(row.id, row.post_id),
                }
        except ValueError:
            values = {"state": "BLOCKED"}
        db.execute(update(targets).where(targets.c.id == row.id).values(**values))
        ledger.event(db, row.id, values["state"].lower(), provider="mock")
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    ledger = configured_ledger()
    provider = os.getenv("PUBLISHER_PROVIDER", "mock")
    if provider not in {"mock", "youtube"}:
        raise SystemExit("PUBLISHER_PROVIDER must be mock or youtube")
    if provider == "youtube":
        from factory.youtube import YouTubePublisher
        from factory.youtube_oauth import (
            AccessTokens,
            OAuthConfig,
            YouTubeError,
            live_enabled,
        )

        if not live_enabled():
            raise SystemExit(
                "YOUTUBE_ENABLED=true is required for the youtube provider"
            )
        try:
            OAuthConfig.load()
        except YouTubeError as exc:
            raise SystemExit(exc.code) from None
        with httpx.Client(
            timeout=float(os.getenv("YOUTUBE_REQUEST_TIMEOUT", "35")), trust_env=False
        ) as http:
            adapter_live = YouTubePublisher(http, AccessTokens(ledger, http))
            run(ledger, adapter_live, args.once)
        return
    run(ledger, MockPublisher(), args.once)


def run(ledger, adapter: Publisher, once=False):
    while True:
        processed = dispatch(ledger, adapter)
        if once:
            return
        if not processed:
            sleep(0.5)


if __name__ == "__main__":
    main()

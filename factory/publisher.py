import argparse
from datetime import timedelta
from time import sleep

from sqlalchemy import select, update

from factory.db import configured_ledger, now, targets
from factory.storage import verified


class MockPublisher:
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


def dispatch(ledger, adapter):
    at = now()
    with ledger.write() as db:
        query = (
            select(targets)
            .where(
                targets.c.due <= at.isoformat(),
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
    adapter = MockPublisher()
    while True:
        processed = dispatch(ledger, adapter)
        if args.once:
            return
        if not processed:
            sleep(0.5)


if __name__ == "__main__":
    main()

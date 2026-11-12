from datetime import timedelta

from sqlalchemy import update

from factory import storage
from factory.db import Ledger, metadata, now, targets
from factory.publisher import MockPublisher, dispatch


def test_publisher_restart_and_idempotency(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "ROOT", tmp_path / "shared")
    url = f"sqlite:///{tmp_path / 'publisher.db'}"
    ledger = Ledger(url)
    metadata.create_all(ledger.engine)
    key = storage.put(b"local media")
    revision = ledger.approve(
        [{"account": "test", "sha256": key, "scheduled_at": now().isoformat()}]
    )
    assert dispatch(ledger, MockPublisher())
    [submitted] = ledger.list_rows(targets)
    assert submitted["state"] == "RECONCILIATION_REQUIRED"
    assert submitted["approval"] == revision
    ledger.engine.dispose()

    restarted = Ledger(url)
    with restarted.write() as db:
        db.execute(
            update(targets).values(due=(now() - timedelta(seconds=1)).isoformat())
        )
    assert dispatch(restarted, MockPublisher())
    assert not dispatch(restarted, MockPublisher())
    [confirmed] = restarted.list_rows(targets)
    assert confirmed["state"] == "PUBLISHED"
    assert confirmed["post_id"] == submitted["post_id"]
    assert confirmed["analytics"]["provenance"] == "mock_analytics"
    restarted.engine.dispose()


def test_publisher_blocks_tampered_media(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "ROOT", tmp_path / "shared")
    ledger = Ledger(f"sqlite:///{tmp_path / 'publisher.db'}")
    metadata.create_all(ledger.engine)
    key = storage.put(b"approved media")
    ledger.approve(
        [{"account": "test", "sha256": key, "scheduled_at": now().isoformat()}]
    )
    storage.path_for(key).write_bytes(b"tampered")
    assert dispatch(ledger, MockPublisher())
    [row] = ledger.list_rows(targets)
    assert row["state"] == "BLOCKED"
    assert row["post_id"] is None
    ledger.engine.dispose()

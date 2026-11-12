import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from factory.db import Conflict, Ledger, metadata


def test_postgres_competing_claims():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration")
    schema = "factory_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    ledger = Ledger(url)
    ledger.engine.update_execution_options(schema_translate_map={None: schema})
    try:
        metadata.create_all(ledger.engine)
        ledger.submit([{"mode": "STORY"}])
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(ledger.claim, ["concurrent-a", "concurrent-b"]))
        [claim] = [result for result in claims if result is not None]
        worker = "concurrent-a" if claims[0] is not None else "concurrent-b"
        ledger.finish(claim["id"], worker, claim["token"], result={"plan": 1})
        with pytest.raises(Conflict):
            ledger.finish(claim["id"], worker, claim["token"], result={"plan": 2})
    finally:
        ledger.engine.dispose()
        with admin.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()

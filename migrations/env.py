from alembic import context

from factory.db import configured_ledger, metadata
from factory.drive_worker import drive_metadata
from sqlalchemy import MetaData
from factory.clips_db import sources
from factory.storage import ROOT

source_metadata = MetaData()
sources.to_metadata(source_metadata)

ROOT.parent.mkdir(parents=True, exist_ok=True)
with configured_ledger().engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=[metadata, drive_metadata, source_metadata],
    )
    with context.begin_transaction():
        context.run_migrations()

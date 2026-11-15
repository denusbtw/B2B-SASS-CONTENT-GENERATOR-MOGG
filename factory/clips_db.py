"""CLIPS registries. Large binaries remain in content-addressed storage."""

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table

clips_metadata = MetaData()
sources = Table(
    "clip_sources",
    clips_metadata,
    Column("sha256", String(64), primary_key=True),
    Column("metadata", JSON, nullable=False),
)
archives = Table(
    "clip_archives",
    clips_metadata,
    Column("job", String(36), primary_key=True),
    Column("folder_id", String(256)),
    Column("folder_confirmed", Integer, default=0, nullable=False),
    Column("state", String(64), nullable=False),
    Column("files", JSON, nullable=False),
    Column("error", String(100)),
    Column("attempts", Integer, default=0, nullable=False),
    Column("due", String(40), nullable=False),
)

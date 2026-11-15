"""Auxiliary OAuth and upload runtime; targets remains the publication ledger."""

from sqlalchemy import JSON, BigInteger, Column, Integer, MetaData, String, Table, Text

youtube_metadata = MetaData()
connections = Table(
    "youtube_connections",
    youtube_metadata,
    Column("id", String(20), primary_key=True),
    Column("channel_id", String(80), nullable=False),
    Column("channel_title", String(300), nullable=False),
    Column("revision", String(36), nullable=False),
    Column("state", String(32), nullable=False),
    Column("credentials", Text, nullable=False),
    Column("updated", String(40), nullable=False),
)
oauth_states = Table(
    "youtube_oauth_states",
    youtube_metadata,
    Column("id", String(64), primary_key=True),
    Column("payload", Text, nullable=False),
    Column("expires", String(40), nullable=False),
)
runtime = Table(
    "publication_runtime",
    youtube_metadata,
    Column("id", String(64), primary_key=True),
    Column("token", String(36)),
    Column("expires", String(40)),
    Column("phase", String(32), nullable=False),
    Column("session", Text),
    Column("offset", BigInteger, nullable=False, default=0),
    Column("size", BigInteger, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("approved_hash", String(64)),
    Column("error", String(100)),
    Column("created", String(40), nullable=False),
    Column("updated", String(40), nullable=False),
    Column("verified", String(40)),
    Column("remote", JSON),
)

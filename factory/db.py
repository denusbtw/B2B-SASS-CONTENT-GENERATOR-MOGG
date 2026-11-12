import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
)
from sqlalchemy import insert, select, text, update

metadata = MetaData()
batches = Table("batches", metadata, Column("id", String(36), primary_key=True))
jobs = Table(
    "jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("batch", ForeignKey("batches.id"), nullable=False, index=True),
    Column("spec", JSON, nullable=False),
    Column("state", String(24), nullable=False),
)
stages = Table(
    "stages",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job", ForeignKey("jobs.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("name", String(24), nullable=False),
    Column("state", String(24), nullable=False, index=True),
    Column("attempt", Integer, nullable=False, default=0),
    Column("worker", String(64)),
    Column("token", String(36)),
    Column("expires", String(40)),
    Column("result", JSON),
    Column("error", String(32)),
)
workers = Table(
    "workers",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("seen", String(40), nullable=False),
)
targets = Table(
    "targets",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("approval", String(36), nullable=False, index=True),
    Column("snapshot", JSON, nullable=False),
    Column("due", String(40), nullable=False, index=True),
    Column("state", String(24), nullable=False, index=True),
    Column("post_id", String(80)),
    Column("analytics", JSON),
)
audit = Table(
    "audit",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("entity", String(64), nullable=False, index=True),
    Column("action", String(40), nullable=False),
    Column("at", String(40), nullable=False),
    Column("detail", JSON, nullable=False),
)
DAGS = {
    "STORY": ("story_plan", "render", "qa", "package"),
    "CLIPS": ("clip_plan", "render", "qa", "package"),
    "CARTOON_2D": ("cartoon_plan", "render", "qa", "package"),
}
LEASE_SECONDS = 30
MAX_ATTEMPTS = 3


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Conflict(ValueError):
    pass


class Ledger:
    def __init__(self, url: str):
        self.engine = create_engine(url, pool_pre_ping=True)
        self.postgres = self.engine.dialect.name == "postgresql"

    @contextmanager
    def write(self):
        with self.engine.begin() as db:
            if not self.postgres:
                db.execute(text("BEGIN IMMEDIATE"))
            yield db

    def lock(self, query, *, skip=False):
        return query.with_for_update(skip_locked=skip) if self.postgres else query

    def event(self, db, entity, action, **detail):
        db.execute(
            insert(audit).values(
                id=uid(),
                entity=entity,
                action=action,
                at=now().isoformat(),
                detail=detail,
            )
        )

    def submit(self, specs):
        batch = uid()
        ids = []
        with self.write() as db:
            db.execute(insert(batches).values(id=batch))
            for spec in specs:
                job = uid()
                ids.append(job)
                db.execute(
                    insert(jobs).values(id=job, batch=batch, spec=spec, state="CREATED")
                )
                for position, name in enumerate(DAGS[spec["mode"]]):
                    db.execute(
                        insert(stages).values(
                            id=uid(),
                            job=job,
                            position=position,
                            name=name,
                            state="READY" if position == 0 else "PENDING",
                        )
                    )
                self.event(db, job, "created")
        return batch, ids

    def list_rows(self, table, *where):
        with self.engine.connect() as db:
            return [
                dict(row) for row in db.execute(select(table).where(*where)).mappings()
            ]

    def touch(self, db, worker, at):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (pg_insert if self.postgres else sqlite_insert)(workers).values(
            id=worker, seen=at
        )
        db.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={"seen": at}))

    def claim(self, worker):
        with self.write() as db:
            at = now().isoformat()
            self.touch(db, worker, at)
            expired = (stages.c.state == "LEASED") & (stages.c.expires <= at)
            exhausted = select(stages).where(expired, stages.c.attempt >= MAX_ATTEMPTS)
            for stale in db.execute(self.lock(exhausted, skip=True)).mappings().all():
                db.execute(
                    update(stages)
                    .where(stages.c.id == stale.id)
                    .values(state="FAILED", error="lease_exhausted", token=None)
                )
                db.execute(
                    update(jobs).where(jobs.c.id == stale.job).values(state="FAILED")
                )
                self.event(db, stale.job, "lease_exhausted", stage=stale.name)
            query = (
                select(stages)
                .where(
                    (stages.c.state == "READY")
                    | (expired & (stages.c.attempt < MAX_ATTEMPTS))
                )
                .order_by(stages.c.position, stages.c.id)
                .limit(1)
            )
            row = db.execute(self.lock(query, skip=True)).mappings().first()
            if row is None:
                return None
            token = uid()
            db.execute(
                update(stages)
                .where(stages.c.id == row.id)
                .values(
                    state="LEASED",
                    token=token,
                    worker=worker,
                    attempt=row.attempt + 1,
                    expires=(now() + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                )
            )
            db.execute(
                update(jobs).where(jobs.c.id == row.job).values(state="GENERATING")
            )
            spec = db.execute(
                select(jobs.c.spec).where(jobs.c.id == row.job)
            ).scalar_one()
            outputs = dict(
                db.execute(
                    select(stages.c.name, stages.c.result).where(
                        stages.c.job == row.job, stages.c.state == "SUCCEEDED"
                    )
                )
                .tuples()
                .all()
            )
            self.event(
                db,
                row.job,
                "claimed",
                stage=row.name,
                worker=worker,
                attempt=row.attempt + 1,
            )
            return {
                "id": row.id,
                "job": row.job,
                "name": row.name,
                "token": token,
                "spec": spec,
                "outputs": outputs,
            }

    def owned(self, db, stage, worker, token):
        row = (
            db.execute(
                self.lock(
                    select(stages).where(
                        stages.c.id == stage,
                        stages.c.worker == worker,
                        stages.c.token == token,
                        stages.c.state == "LEASED",
                        stages.c.expires > now().isoformat(),
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise Conflict("Stage lease is stale or belongs to another worker")
        return row

    def heartbeat(self, stage, worker, token):
        with self.write() as db:
            self.owned(db, stage, worker, token)
            self.touch(db, worker, now().isoformat())
            db.execute(
                update(stages)
                .where(stages.c.id == stage)
                .values(expires=(now() + timedelta(seconds=LEASE_SECONDS)).isoformat())
            )

    def finish(self, stage, worker, token, result=None, error=None):
        with self.write() as db:
            row = self.owned(db, stage, worker, token)
            state = "SUCCEEDED"
            if error:
                state = (
                    "READY"
                    if error == "transient" and row.attempt < MAX_ATTEMPTS
                    else "FAILED"
                )
                if state == "FAILED":
                    job_state = (
                        "REVIEW_REQUIRED" if error == "review_required" else "FAILED"
                    )
                    db.execute(
                        update(jobs).where(jobs.c.id == row.job).values(state=job_state)
                    )
            elif row.name == "package":
                job_state = "COMPLETE" if result["publishable"] else "BLOCKED"
                db.execute(
                    update(jobs).where(jobs.c.id == row.job).values(state=job_state)
                )
            else:
                db.execute(
                    update(stages)
                    .where(
                        stages.c.job == row.job,
                        stages.c.position == row.position + 1,
                        stages.c.state == "PENDING",
                    )
                    .values(state="READY")
                )
            db.execute(
                update(stages)
                .where(stages.c.id == stage)
                .values(state=state, token=None, result=result, error=error)
            )
            self.event(
                db, row.job, state.lower(), stage=row.name, worker=worker, error=error
            )

    def approve(self, snapshots):
        revision = uid()
        with self.write() as db:
            for snap in snapshots:
                key = digest([snap["account"], snap["sha256"], snap["scheduled_at"]])
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                statement = (pg_insert if self.postgres else sqlite_insert)(targets)
                statement = (
                    statement.values(
                        id=key,
                        approval=revision,
                        snapshot=snap,
                        due=snap["scheduled_at"],
                        state="SCHEDULED",
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                    .returning(targets.c.id)
                )
                if db.execute(statement).scalar_one_or_none() is None:
                    raise Conflict(
                        "Media already scheduled for this account and time slot"
                    )
                self.event(
                    db, key, "approved", revision=revision, sha256=snap["sha256"]
                )
        return revision


def configured_ledger() -> Ledger:
    return Ledger(os.getenv("DATABASE_URL", "sqlite:///data/local.db"))

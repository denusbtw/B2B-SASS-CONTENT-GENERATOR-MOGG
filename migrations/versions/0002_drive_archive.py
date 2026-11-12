from alembic import op
from sqlalchemy import Column, Integer, String

revision = "0002_drive_archive"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "drive_settings",
        Column("key", String(20), primary_key=True),
        Column("file_id", String(256), nullable=False),
        Column("confirmed", Integer, nullable=False),
    )
    op.create_table(
        "drive_scanned_jobs", Column("job_id", String(36), primary_key=True)
    )
    op.create_table(
        "drive_uploads",
        Column("sha256", String(64), primary_key=True),
        Column("job_id", String(36), nullable=False),
        Column("part", Integer, nullable=False),
        Column("remote_id", String(256)),
        Column("url", String(300)),
        Column("error", String(80)),
        Column("attempts", Integer, nullable=False),
        Column("due", String(40), nullable=False),
    )
    op.create_index("ix_drive_uploads_due", "drive_uploads", ["due"])


def downgrade():
    op.drop_index("ix_drive_uploads_due", table_name="drive_uploads")
    op.drop_table("drive_uploads")
    op.drop_table("drive_scanned_jobs")
    op.drop_table("drive_settings")

from alembic import op
from factory.clips_db import clips_metadata

revision = "0003_real_clips"
down_revision = "0002_drive_archive"
branch_labels = None
depends_on = None


def upgrade():
    clips_metadata.create_all(op.get_bind())


def downgrade():
    clips_metadata.drop_all(op.get_bind())

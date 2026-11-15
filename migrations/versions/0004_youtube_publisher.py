from alembic import op
from factory.youtube_db import youtube_metadata

revision = "0004_youtube_publisher"
down_revision = "0003_real_clips"
branch_labels = None
depends_on = None


def upgrade():
    youtube_metadata.create_all(op.get_bind())


def downgrade():
    youtube_metadata.drop_all(op.get_bind())

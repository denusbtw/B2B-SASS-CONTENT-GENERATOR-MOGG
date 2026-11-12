from alembic import op

from factory.db import metadata

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    metadata.drop_all(op.get_bind(), checkfirst=False)

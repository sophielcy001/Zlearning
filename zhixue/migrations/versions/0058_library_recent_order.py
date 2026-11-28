from alembic import op
import sqlalchemy as sa


revision = "0058_library_recent_order"
down_revision = "0057_local_memories"


def upgrade():
    op.add_column("library_file_activity", sa.Column("last_viewed_order", sa.Integer()))
    op.create_index("ix_library_file_activity_order", "library_file_activity", ["last_viewed_order"])


def downgrade():
    op.drop_index("ix_library_file_activity_order", table_name="library_file_activity")
    op.drop_column("library_file_activity", "last_viewed_order")
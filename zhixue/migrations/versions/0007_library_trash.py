import sqlalchemy as sa
from alembic import op


revision = "0007_library_trash"
down_revision = "0006_library_locations"


def upgrade():
    op.create_table(
        "library_trash",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("project_id", sa.String()),
        sa.Column("project_topic", sa.String()),
        sa.Column("original_path", sa.String(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("file_key", sa.String()),
        sa.Column("birth_time", sa.String()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("restore_path", sa.String()),
        sa.Column("error", sa.String()),
    )
    op.create_index("ix_library_trash_file_id", "library_trash", ["file_id"])
    op.create_index("ix_library_trash_state", "library_trash", ["state"])
    op.add_column("library_locations", sa.Column("trash_id", sa.String(36)))


def downgrade():
    op.drop_column("library_locations", "trash_id")
    op.drop_table("library_trash")

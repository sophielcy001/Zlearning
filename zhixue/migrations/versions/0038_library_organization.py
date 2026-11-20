from alembic import op
import sqlalchemy as sa


revision = "0038_library_organization"
down_revision = "0037_library_folders"


def upgrade():
    op.create_table(
        "library_file_tags",
        sa.Column("file_id", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
    )
    op.create_index("ix_library_file_tags_key", "library_file_tags", ["key"])
    op.create_table(
        "library_file_activity",
        sa.Column("file_id", sa.Text(), primary_key=True),
        sa.Column("favorite", sa.Boolean(), nullable=False),
        sa.Column("last_viewed_at", sa.String()),
    )
    op.create_table(
        "library_file_copies",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("directory_key", sa.Text(), nullable=False),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("file_key", sa.Text(), nullable=False),
        sa.Column("birth_time", sa.Text()),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("created_on", sa.Date(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_library_file_copies_state", "library_file_copies", ["state"])


def downgrade():
    op.drop_table("library_file_copies")
    op.drop_table("library_file_activity")
    op.drop_table("library_file_tags")

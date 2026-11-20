import sqlalchemy as sa
from alembic import op


revision = "0006_library_locations"
down_revision = "0005_note_versions"


def upgrade():
    op.create_table(
        "library_locations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("path", sa.String(), nullable=False, unique=True),
        sa.Column("file_key", sa.String()),
        sa.Column("birth_time", sa.String()),
        sa.Column("revision", sa.String(64)),
        sa.Column("missing", sa.Boolean(), nullable=False),
    )
    op.add_column("note_writes", sa.Column("target_path", sa.String()))


def downgrade():
    op.drop_column("note_writes", "target_path")
    op.drop_table("library_locations")

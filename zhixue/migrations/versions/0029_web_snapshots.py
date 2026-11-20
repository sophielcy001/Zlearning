from alembic import op
import sqlalchemy as sa


revision = "0029_web_snapshots"
down_revision = "0028_library_ocr"


def upgrade():
    op.create_table(
        "library_web_snapshots",
        sa.Column("file_id", sa.String(), sa.ForeignKey("imports.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.String(), nullable=False),
        sa.Column("encoding", sa.String(), nullable=False),
        sa.Column("revision", sa.String(), nullable=False),
    )
    op.create_index("ix_library_web_snapshots_url_revision", "library_web_snapshots", ["url", "revision"])


def downgrade():
    op.drop_table("library_web_snapshots")

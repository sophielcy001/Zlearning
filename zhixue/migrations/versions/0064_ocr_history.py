from alembic import op
import sqlalchemy as sa


revision = "0064_ocr_history"
down_revision = "0063_local_practice"


def upgrade():
    op.create_table(
        "library_ocr_versions",
        sa.Column("file_id", sa.String(36), sa.ForeignKey("imports.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("page", sa.Integer(), primary_key=True),
        sa.Column("text_revision", sa.String(), primary_key=True),
        sa.Column("source_revision", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("saved_at", sa.DateTime(), nullable=True),
    )
    op.execute("""
        INSERT INTO library_ocr_versions (file_id, page, text_revision, source_revision, body)
        SELECT file_id, page, text_revision, revision, body FROM library_ocr_pages
    """)


def downgrade():
    op.drop_table("library_ocr_versions")

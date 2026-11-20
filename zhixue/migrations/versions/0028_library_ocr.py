from alembic import op
import sqlalchemy as sa


revision = "0028_library_ocr"
down_revision = "0027_learning_failover"


def upgrade():
    op.create_table(
        "library_ocr_pages",
        sa.Column("file_id", sa.String(), sa.ForeignKey("imports.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("page", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.String(), nullable=False),
        sa.Column("text_revision", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
    )
    op.add_column("library_passages", sa.Column("text_revision", sa.String()))


def downgrade():
    op.execute("DELETE FROM library_index_documents WHERE id IN (SELECT file_id FROM library_ocr_pages)")
    op.drop_column("library_passages", "text_revision")
    op.drop_table("library_ocr_pages")

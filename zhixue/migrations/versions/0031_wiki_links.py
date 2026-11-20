from alembic import op
import sqlalchemy as sa


revision = "0031_wiki_links"
down_revision = "0030_note_catalog"


def upgrade():
    op.add_column("library_index_documents", sa.Column(
        "wiki_indexed", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.create_table(
        "library_wiki_names",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("file_id", sa.Text(), sa.ForeignKey("library_index_documents.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_library_wiki_names_file_id", "library_wiki_names", ["file_id"])
    op.create_table(
        "library_wiki_links",
        sa.Column("source_id", sa.Text(), sa.ForeignKey("library_index_documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("target_id", sa.Text()),
    )
    op.create_index("ix_library_wiki_links_key", "library_wiki_links", ["key"])
    op.create_index("ix_library_wiki_links_target_id", "library_wiki_links", ["target_id"])


def downgrade():
    op.drop_table("library_wiki_links")
    op.drop_table("library_wiki_names")
    op.drop_column("library_index_documents", "wiki_indexed")

from alembic import op
import sqlalchemy as sa


revision = "0037_library_folders"
down_revision = "0036_graph_recommendations"


def upgrade():
    op.create_table(
        "library_wiki_aliases",
        sa.Column("file_id", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
    )


def downgrade():
    op.drop_table("library_wiki_aliases")

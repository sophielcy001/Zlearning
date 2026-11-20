from alembic import op
import sqlalchemy as sa


revision = "0035_library_graph"
down_revision = "0034_assessment_notes"


def upgrade():
    op.create_table(
        "library_graph",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("head", sa.Integer()),
        sa.Column("redo", sa.JSON(), nullable=False),
        sa.Column("anchors", sa.JSON(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("view", sa.JSON(), nullable=False),
    )
    op.create_table(
        "library_graph_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent", sa.Integer()),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("library_graph_revisions")
    op.drop_table("library_graph")

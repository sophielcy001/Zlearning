from alembic import op
import sqlalchemy as sa


revision = "0032_note_suggestions"
down_revision = "0031_wiki_links"


def upgrade():
    op.create_table(
        "note_suggestions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("note_id", sa.String(36), sa.ForeignKey("notes.id", ondelete="SET NULL")),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("connection", sa.JSON(), nullable=False),
        sa.Column("context", sa.JSON()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("result", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("billing_day", sa.Date()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
    )
    op.create_index("ix_note_suggestions_note_id", "note_suggestions", ["note_id"])
    op.create_index("ix_note_suggestions_queue", "note_suggestions", ["state", "created_at"])
    op.create_index("ix_note_suggestions_billing_day", "note_suggestions", ["billing_day"])
    op.create_index("ix_note_suggestions_finished_at", "note_suggestions", ["finished_at"])


def downgrade():
    op.drop_table("note_suggestions")

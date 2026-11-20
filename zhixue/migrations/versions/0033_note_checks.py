from alembic import op
import sqlalchemy as sa


revision = "0033_note_checks"
down_revision = "0032_note_suggestions"


def upgrade():
    op.create_table(
        "note_check_plans",
        sa.Column("note_id", sa.String(36), sa.ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(36), nullable=False, unique=True),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("recipient", sa.JSON(), nullable=False),
        sa.Column("lesson_id", sa.String(36)),
        sa.Column("course", sa.JSON()),
        sa.Column("revision", sa.String(64)),
        sa.Column("signature", sa.Text()),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("problem", sa.String()),
        sa.Column("blocked_on", sa.Date()),
    )
    op.create_table(
        "note_check_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("end", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.String()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("billing_day", sa.Date()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.UniqueConstraint("plan_id", "revision", "start"),
    )
    op.create_index("ix_note_check_batches_cache", "note_check_batches", ["plan_id", "digest", "state"])
    op.create_index("ix_note_check_batches_billing_day", "note_check_batches", ["billing_day"])
    op.create_index("ix_note_check_batches_finished_at", "note_check_batches", ["finished_at"])


def downgrade():
    op.drop_table("note_check_batches")
    op.drop_table("note_check_plans")

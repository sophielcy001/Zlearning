from alembic import op
import sqlalchemy as sa


revision = "0025_stage_model_review"
down_revision = "0024_stage_assessments"


def upgrade():
    op.create_table(
        "stage_reviews",
        sa.Column("id", sa.String(36), sa.ForeignKey("stage_assessment_attempts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("max_calls", sa.Integer()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("active_call", sa.String(36)),
        sa.Column("held_call", sa.String(36)),
        sa.Column("daily_blocked_on", sa.Date()),
    )
    op.create_index("ix_stage_reviews_queue", "stage_reviews", ["state", "available_at", "created_at"])
    op.create_table(
        "stage_review_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("review_id", sa.String(36), sa.ForeignKey("stage_reviews.id", ondelete="CASCADE"), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("billing_day", sa.Date(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("auto_retry", sa.Boolean(), nullable=False),
        sa.Column("report", sa.JSON()),
    )
    for column in ("review_id", "billing_day", "finished_at"):
        op.create_index(f"ix_stage_review_calls_{column}", "stage_review_calls", [column])


def downgrade():
    op.drop_table("stage_review_calls")
    op.drop_table("stage_reviews")

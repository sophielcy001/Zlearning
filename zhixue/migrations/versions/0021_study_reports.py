from alembic import op
import sqlalchemy as sa


revision = "0021_study_reports"
down_revision = "0020_study_plan_locks"


def upgrade():
    op.add_column("timers", sa.Column("category", sa.String(), server_default="unclassified", nullable=False))
    op.add_column("timers", sa.Column("review_key", sa.String()))
    op.add_column("timers", sa.Column("started_on", sa.Date()))
    op.create_table(
        "timer_spans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timer_id", sa.String(36), sa.ForeignKey("timers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("began_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.CheckConstraint("seconds > 0", name="ck_timer_spans_seconds"),
    )
    op.create_index("ix_timer_spans_timer_day", "timer_spans", ["timer_id", "day"])
    op.create_index("ix_timer_spans_day", "timer_spans", ["day"])
    op.create_table(
        "timer_corrections",
        sa.Column("timer_id", sa.String(36), sa.ForeignKey("timers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_key", sa.String(10), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("report_on", sa.Date()),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("saved_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("seconds >= 0", name="ck_timer_corrections_seconds"),
    )
    op.create_table(
        "study_plan_snapshots",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("budget_minutes", sa.Integer(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("frozen", sa.Boolean(), nullable=False),
    )


def downgrade():
    op.drop_table("study_plan_snapshots")
    op.drop_table("timer_corrections")
    op.drop_table("timer_spans")
    op.drop_column("timers", "started_on")
    op.drop_column("timers", "review_key")
    op.drop_column("timers", "category")

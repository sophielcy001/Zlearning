from alembic import op
import sqlalchemy as sa


revision = "0022_local_reminders"
down_revision = "0021_study_reports"


def upgrade():
    op.create_table(
        "reminder_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("quiet_start", sa.Integer()),
        sa.Column("quiet_end", sa.Integer()),
    )
    op.create_table(
        "reminder_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject", sa.String(100), nullable=False, unique=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE")),
        sa.Column("sent_on", sa.Date(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String()),
        sa.Column("snooze_at", sa.DateTime()),
        sa.Column("snooze_on", sa.Date()),
    )
    op.create_index("ix_reminder_receipts_lesson_id", "reminder_receipts", ["lesson_id"])


def downgrade():
    op.drop_table("reminder_receipts")
    op.drop_table("reminder_settings")

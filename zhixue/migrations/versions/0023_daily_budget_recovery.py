from alembic import op
import sqlalchemy as sa


revision = "0023_daily_budget_recovery"
down_revision = "0022_local_reminders"
BLOCKED_TABLES = ("model_studies", "assessment_jobs", "lesson_questions", "lesson_consolidations", "prerequisite_tasks")


def upgrade():
    op.add_column("generation_settings", sa.Column("auto_continue_daily_budget", sa.Boolean(), server_default=sa.false(), nullable=False))
    for table in BLOCKED_TABLES:
        op.add_column(table, sa.Column("daily_blocked_on", sa.Date()))


def downgrade():
    for table in BLOCKED_TABLES:
        op.drop_column(table, "daily_blocked_on")
    op.drop_column("generation_settings", "auto_continue_daily_budget")

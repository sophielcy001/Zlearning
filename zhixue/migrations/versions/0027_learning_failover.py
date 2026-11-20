from alembic import op
import sqlalchemy as sa


revision = "0027_learning_failover"
down_revision = "0026_model_failover"

COLUMNS = (
    ("assessment_plans", "backups", sa.JSON()),
    ("assessment_jobs", "failovers", sa.JSON()),
    ("lesson_assistant_plans", "backups", sa.JSON()),
    ("lesson_questions", "failovers", sa.JSON()),
    ("lesson_consolidation_plans", "backups", sa.JSON()),
    ("lesson_consolidations", "failovers", sa.JSON()),
    ("prerequisite_tasks", "failovers", sa.JSON()),
    ("prerequisite_calls", "failover_reason", sa.String()),
)


def upgrade():
    for table, name, kind in COLUMNS:
        op.add_column(table, sa.Column(name, kind))


def downgrade():
    for table, name, _ in reversed(COLUMNS):
        op.drop_column(table, name)

from alembic import op
import sqlalchemy as sa


revision = "0026_model_failover"
down_revision = "0025_stage_model_review"

COLUMNS = (
    ("model_studies", "backup", sa.JSON()),
    ("course_review_plans", "backup", sa.JSON()),
    ("model_uses", "failover_reason", sa.String()),
    ("stage_reviews", "failover_reason", sa.String()),
    ("stage_review_calls", "connection", sa.JSON()),
    ("stage_review_calls", "failover_reason", sa.String()),
)


def upgrade():
    for table, name, kind in COLUMNS:
        op.add_column(table, sa.Column(name, kind))


def downgrade():
    for table, name, _ in reversed(COLUMNS):
        op.drop_column(table, name)

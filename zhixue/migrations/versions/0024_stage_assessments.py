from alembic import op
import sqlalchemy as sa


revision = "0024_stage_assessments"
down_revision = "0023_daily_budget_recovery"


def upgrade():
    op.create_table(
        "stage_assessments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phase_key", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("route_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("skipped_at", sa.DateTime()),
        sa.UniqueConstraint("project_id", "phase_key"),
    )
    op.create_table(
        "stage_assessment_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("assessment_id", sa.String(36), sa.ForeignKey("stage_assessments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("submission", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.UniqueConstraint("assessment_id", "number"),
    )


def downgrade():
    op.drop_table("stage_assessment_attempts")
    op.drop_table("stage_assessments")

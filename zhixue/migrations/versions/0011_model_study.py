from alembic import op
import sqlalchemy as sa


revision = "0011_model_study"
down_revision = "0010_study_intake"


def upgrade():
    op.add_column("lessons", sa.Column("outline", sa.JSON(none_as_null=True)))
    op.create_table(
        "model_studies",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("connection_revision", sa.Integer(), nullable=False),
        sa.Column("goal_fields", sa.JSON(), nullable=False),
        sa.Column("max_calls", sa.Integer()),
        sa.Column("focus_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="SET NULL")),
        sa.Column("stopped", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String()),
        sa.Column("deleted_at", sa.DateTime()),
    )
    op.create_table(
        "generation_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("model_studies.project_id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), unique=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer()),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("active_call", sa.String(36)),
        sa.Column("result", sa.JSON(none_as_null=True)),
        sa.Column("error", sa.String()),
    )
    op.create_index("ix_generation_steps_project_id", "generation_steps", ["project_id"])
    op.create_index("ix_generation_steps_priority", "generation_steps", ["priority"])
    op.create_table(
        "model_uses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("model_studies.project_id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", sa.String(36), sa.ForeignKey("generation_steps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("billing_day", sa.Date(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("auto_retry", sa.Boolean(), nullable=False),
    )
    for field in ("project_id", "billing_day", "finished_at"):
        op.create_index(f"ix_model_uses_{field}", "model_uses", [field])
    op.create_table(
        "generation_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("daily_limit", sa.Integer()),
        sa.Column("concurrency", sa.Integer(), nullable=False),
    )


def downgrade():
    for name in ("generation_settings", "model_uses", "generation_steps", "model_studies"):
        op.drop_table(name)
    op.drop_column("lessons", "outline")

from alembic import op
import sqlalchemy as sa


revision = "0017_lesson_consolidation"
down_revision = "0016_lesson_reading"


def upgrade():
    op.add_column("model_uses", sa.Column("consolidation_id", sa.String(36)))
    op.create_index("ix_model_uses_consolidation_id", "model_uses", ["consolidation_id"])
    op.create_table(
        "lesson_consolidation_plans",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("main", sa.JSON(), nullable=False),
        sa.Column("reviewer", sa.JSON(), nullable=False),
        sa.Column("proofs", sa.JSON(), nullable=False),
        sa.Column("max_calls", sa.Integer()),
    )
    op.create_table(
        "lesson_consolidations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.String(36)),
        sa.Column("course_version", sa.Integer(), nullable=False),
        sa.Column("question_ids", sa.JSON(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("active_call", sa.String(36)),
        sa.Column("material", sa.JSON(none_as_null=True)),
        sa.Column("draft", sa.JSON(none_as_null=True)),
        sa.Column("finding", sa.JSON(none_as_null=True)),
        sa.Column("author", sa.JSON(none_as_null=True)),
        sa.Column("reviewer", sa.JSON(none_as_null=True)),
        sa.Column("published_version", sa.Integer()),
    )
    op.create_index("ix_lesson_consolidations_lesson_id", "lesson_consolidations", ["lesson_id"])
    op.create_index("ix_lesson_consolidations_queue", "lesson_consolidations", ["state", "available_at", "created_at"])


def downgrade():
    op.drop_table("lesson_consolidations")
    op.drop_table("lesson_consolidation_plans")
    op.drop_index("ix_model_uses_consolidation_id", table_name="model_uses")
    op.drop_column("model_uses", "consolidation_id")

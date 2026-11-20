from alembic import op
import sqlalchemy as sa


revision = "0015_lesson_assistant"
down_revision = "0014_review_remediation"


def upgrade():
    op.add_column("model_uses", sa.Column("question_id", sa.String(36)))
    op.create_index("ix_model_uses_question_id", "model_uses", ["question_id"])
    op.create_table(
        "lesson_assistant_plans",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), primary_key=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("recipient", sa.JSON(), nullable=False),
        sa.Column("proofs", sa.JSON(), nullable=False),
        sa.Column("max_calls", sa.Integer()),
    )
    op.create_table(
        "lesson_questions",
        sa.Column("id", sa.String(36), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("reply_id", sa.String(36), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("selection", sa.JSON(none_as_null=True)),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("active_call", sa.String(36)),
        sa.Column("error", sa.String()),
        sa.Column("favorite", sa.Boolean(), nullable=False),
        sa.Column("support", sa.String()),
        sa.Column("citations", sa.JSON(none_as_null=True)),
        sa.Column("recipient", sa.JSON(none_as_null=True)),
    )
    op.create_index("ix_lesson_questions_lesson_id", "lesson_questions", ["lesson_id"])
    op.create_index("ix_lesson_questions_queue", "lesson_questions", ["state", "available_at", "created_at"])


def downgrade():
    op.drop_table("lesson_questions")
    op.drop_table("lesson_assistant_plans")
    op.drop_index("ix_model_uses_question_id", table_name="model_uses")
    op.drop_column("model_uses", "question_id")

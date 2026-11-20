from alembic import op
import sqlalchemy as sa


revision = "0016_lesson_reading"
down_revision = "0015_lesson_assistant"


def upgrade():
    op.create_table(
        "lesson_reading_positions",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("point", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("lesson_reading_positions")

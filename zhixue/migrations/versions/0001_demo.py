import sqlalchemy as sa
from alembic import op


revision = "0001_demo"
down_revision = None


def upgrade():
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("foundation", sa.String(), nullable=False),
        sa.Column("sample", sa.Boolean(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("created_on", sa.Date(), nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
    )
    op.create_table(
        "lessons",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("learned_on", sa.Date()),
        sa.UniqueConstraint("project_id", "position"),
    )
    op.create_index("ix_lessons_project_id", "lessons", ["project_id"])
    op.create_table(
        "reviews",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("due_on", sa.Date()),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("hinted", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "attempts",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("round_number", sa.Integer(), primary_key=True),
        sa.Column("submitted_on", sa.Date(), nullable=False),
        sa.Column("previous_due_on", sa.Date(), nullable=False),
        sa.Column("course_version", sa.Integer(), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )
    op.create_table(
        "imports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_on", sa.Date(), nullable=False),
    )
    op.create_table(
        "demo_clock",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("offset", sa.Integer(), nullable=False),
    )


def downgrade():
    for table in ("demo_clock", "imports", "attempts", "reviews", "lessons", "tasks", "projects"):
        op.drop_table(table)

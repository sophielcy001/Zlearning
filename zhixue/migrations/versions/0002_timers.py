import sqlalchemy as sa
from alembic import op


revision = "0002_timers"
down_revision = "0001_demo"


def upgrade():
    op.create_table(
        "timers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("elapsed", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("timers")

import sqlalchemy as sa
from alembic import op


revision = "0044_reminder_push"
down_revision = "0043_reminder_delivery"


def upgrade():
    op.create_table(
        "reminder_push_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject", sa.String(100), nullable=False, unique=True),
        sa.Column("workspace", sa.String(36), nullable=False),
        sa.Column("receiver", sa.String(64), nullable=False),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE")),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("prior_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("reminder_push_claims")

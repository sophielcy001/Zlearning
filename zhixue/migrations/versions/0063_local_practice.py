from alembic import op
import sqlalchemy as sa


revision = "0063_local_practice"
down_revision = "0062_repository_mcp"


def upgrade():
    op.create_table(
        "local_practice_attempts",
        sa.Column("lesson_id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("source_round", sa.Integer(), primary_key=True),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("view", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["lesson_id", "key", "source_round"], ["attempts.lesson_id", "attempts.key", "attempts.round_number"],
            ondelete="CASCADE",
        ),
    )


def downgrade():
    op.drop_table("local_practice_attempts")

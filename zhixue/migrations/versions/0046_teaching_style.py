from alembic import op
import sqlalchemy as sa


revision = "0046_teaching_style"
down_revision = "0045_chat_model"


def upgrade():
    op.create_table(
        "teaching_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("style", sa.String(16), nullable=False),
        sa.CheckConstraint("style IN ('direct', 'guided')", name="teaching_style_supported"),
    )
    op.add_column("chat_replies", sa.Column("style", sa.String(16), server_default="direct", nullable=False))


def downgrade():
    op.drop_column("chat_replies", "style")
    op.drop_table("teaching_preferences")

from alembic import op
import sqlalchemy as sa


revision = "0057_local_memories"
down_revision = "0056_agent_authoring"


def upgrade():
    op.create_table(
        "local_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("text", sa.Text()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("professional_agents.id")),
        sa.Column("source_message_id", sa.String(36), unique=True),
        sa.Column("conversation_id", sa.String(36)),
        sa.Column("review_on", sa.Date()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('active', 'pending', 'deleted')", name="memory_state"),
    )
    op.create_index("ix_local_memories_review", "local_memories", ["state", "review_on"])


def downgrade():
    op.drop_table("local_memories")
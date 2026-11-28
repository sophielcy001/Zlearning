from alembic import op
import sqlalchemy as sa


revision = "0055_professional_agents"
down_revision = "0054_skill_versions"


def upgrade():
    op.create_table(
        "professional_agents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("confirmation", sa.String(64), nullable=False),
    )
    op.add_column("chat_replies", sa.Column("agent_scope", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("chat_replies", "agent_scope")
    op.drop_table("professional_agents")

from alembic import op
import sqlalchemy as sa


revision = "0056_agent_authoring"
down_revision = "0055_professional_agents"


def upgrade():
    op.create_table(
        "agent_authoring_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("connection", sa.JSON(), nullable=False),
        sa.Column("scope", sa.JSON()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("daily_blocked_on", sa.Date()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("billing_day", sa.Date()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
    )
    op.create_index("ix_agent_authoring_queue", "agent_authoring_jobs", ["state", "created_at"])
    for column in ("billing_day", "finished_at"):
        op.create_index(f"ix_agent_authoring_jobs_{column}", "agent_authoring_jobs", [column])


def downgrade():
    op.drop_table("agent_authoring_jobs")

from alembic import op
import sqlalchemy as sa


revision = "0062_repository_mcp"
down_revision = "0061_external_memory"


def upgrade():
    op.create_table(
        "repository_mcp_connections",
        sa.Column("provider", sa.String(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("repositories", sa.JSON(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=False),
        sa.Column("version", sa.String()),
        sa.Column("credential_ref", sa.String(36)),
        sa.Column("credential_hash", sa.String(64)),
    )


def downgrade():
    op.drop_table("repository_mcp_connections")

from alembic import op
import sqlalchemy as sa


revision = "0061_external_memory"
down_revision = "0060_auto_memory"


def upgrade():
    op.create_table(
        "external_memory_connection",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(36), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
    )
    op.create_table(
        "external_memory_copies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("memory_id", sa.String(36), nullable=False),
        sa.Column("memory_revision", sa.Integer(), nullable=False),
        sa.Column("memory_scope", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("remote_ids", sa.JSON(), nullable=False),
        sa.Column("delete_requested", sa.Boolean(), nullable=False),
        sa.Column("attempt", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_external_memory_copies_memory_id", "external_memory_copies", ["memory_id"])
    op.create_index("ix_external_memory_queue", "external_memory_copies", ["attempt", "state", "created_at"])


def downgrade():
    op.drop_table("external_memory_copies")
    op.drop_table("external_memory_connection")

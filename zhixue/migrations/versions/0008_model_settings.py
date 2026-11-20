import sqlalchemy as sa
from alembic import op


revision = "0008_model_settings"
down_revision = "0007_library_trash"


def upgrade():
    op.create_table(
        "model_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("protocol", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("credential_ref", sa.String(36)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("test_token", sa.String(36)),
        sa.Column("last_test", sa.JSON(none_as_null=True)),
    )
    op.create_table(
        "model_roles",
        sa.Column("role", sa.String(), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("model_connections.id"), nullable=False),
    )
    op.create_index("ix_model_roles_connection_id", "model_roles", ["connection_id"])
    op.create_table(
        "model_credential_pending",
        sa.Column("reference", sa.String(36), primary_key=True),
        sa.Column("reserved", sa.Boolean(), nullable=False),
    )


def downgrade():
    op.drop_table("model_credential_pending")
    op.drop_table("model_roles")
    op.drop_table("model_connections")

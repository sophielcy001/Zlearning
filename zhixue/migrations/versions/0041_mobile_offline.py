from uuid import uuid4

import sqlalchemy as sa
from alembic import op


revision = "0041_mobile_offline"
down_revision = "0040_semantic_passages"


def upgrade():
    workspace = op.create_table(
        "mobile_workspace",
        sa.Column("singleton", sa.Integer, primary_key=True),
        sa.Column("identity", sa.String(36), nullable=False),
        sa.CheckConstraint("singleton=1"),
    )
    op.bulk_insert(workspace, [{"singleton": 1, "identity": str(uuid4())}])
    op.create_table(
        "mobile_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("received_at", sa.DateTime, nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("reply", sa.JSON),
    )


def downgrade():
    op.drop_table("mobile_changes")
    op.drop_table("mobile_workspace")

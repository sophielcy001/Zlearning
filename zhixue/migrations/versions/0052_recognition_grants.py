from alembic import op
import sqlalchemy as sa


revision = "0052_recognition_grants"
down_revision = "0051_course_anchor_links"


def upgrade():
    op.add_column("model_charges", sa.Column("audio_seconds", sa.Float(), nullable=True))
    op.create_table(
        "recognition_grants",
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=True),
        sa.Column("connection_revision", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("receipt", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["model_connections.id"], ondelete="SET NULL"),
    )


def downgrade():
    op.drop_table("recognition_grants")
    op.drop_column("model_charges", "audio_seconds")

from alembic import op
import sqlalchemy as sa


revision = "0060_auto_memory"
down_revision = "0059_memory_inference"


def upgrade():
    op.add_column("memory_inference_jobs", sa.Column("automatic", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("memory_inference_jobs", "automatic")

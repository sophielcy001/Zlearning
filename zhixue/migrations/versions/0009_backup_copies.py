import sqlalchemy as sa
from alembic import op


revision = "0009_backup_copies"
down_revision = "0008_model_settings"


def upgrade():
    op.create_table(
        "backup_file_origins",
        sa.Column("file_id", sa.String(36), primary_key=True),
        sa.Column("backup_id", sa.String(36), nullable=False),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("source_revision", sa.String(64), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_on", sa.Date(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
    )


def downgrade():
    op.drop_table("backup_file_origins")

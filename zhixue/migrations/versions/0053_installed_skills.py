from alembic import op
import sqlalchemy as sa


revision = "0053_installed_skills"
down_revision = "0052_recognition_grants"


def upgrade():
    op.create_table(
        "installed_skills",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("installed_skills")

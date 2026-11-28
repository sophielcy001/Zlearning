from alembic import op
import sqlalchemy as sa


revision = "0054_skill_versions"
down_revision = "0053_installed_skills"


def upgrade():
    op.add_column("installed_skills", sa.Column("activation", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "skill_versions",
        sa.Column("name", sa.String(64), sa.ForeignKey("installed_skills.name", ondelete="CASCADE"), primary_key=True),
        sa.Column("checksum", sa.String(64), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.UniqueConstraint("name", "position"),
    )
    op.execute(
        "INSERT INTO skill_versions (name, checksum, position, source, manifest) "
        "SELECT name, checksum, 1, source, manifest FROM installed_skills"
    )


def downgrade():
    op.drop_table("skill_versions")
    op.drop_column("installed_skills", "activation")

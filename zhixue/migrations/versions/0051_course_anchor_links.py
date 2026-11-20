from alembic import op
import sqlalchemy as sa


revision = "0051_course_anchor_links"
down_revision = "0050_course_revision"


def upgrade():
    op.create_table(
        "course_anchor_links",
        sa.Column("lesson_id", sa.String(36), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_section", sa.Integer(), nullable=False),
        sa.Column("source_start", sa.Integer(), nullable=False),
        sa.Column("source_end", sa.Integer(), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("target_section", sa.Integer(), nullable=False),
        sa.Column("target_start", sa.Integer(), nullable=False),
        sa.Column("target_end", sa.Integer(), nullable=False),
        sa.Column("mapping_revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("lesson_id", "source_version", "source_section", "source_start", "source_end"),
        sa.ForeignKeyConstraint(["lesson_id", "source_version"], ["course_versions.lesson_id", "course_versions.version"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lesson_id", "target_version"], ["course_versions.lesson_id", "course_versions.version"], ondelete="CASCADE"),
    )


def downgrade():
    op.drop_table("course_anchor_links")

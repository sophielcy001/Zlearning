from alembic import op
import sqlalchemy as sa


revision = "0012_course_verification"
down_revision = "0011_model_study"


def upgrade():
    op.add_column("generation_steps", sa.Column("finding_id", sa.String(36)))
    op.add_column("generation_steps", sa.Column("publication_pending", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("generation_steps", sa.Column("manual_review", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("model_uses", sa.Column("phase", sa.String()))
    op.create_table(
        "course_review_plans",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("model_studies.project_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("reviewer", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
    )
    op.create_table(
        "course_findings",
        sa.Column("id", sa.String(36), sa.ForeignKey("model_uses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("reviewer", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
    )
    op.create_index("ix_course_findings_lesson_id", "course_findings", ["lesson_id"])
    op.create_table(
        "course_versions",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("finding_id", sa.String(36), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("markdown", sa.String(), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
    )
    op.create_table(
        "course_dispositions",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("section_index", sa.Integer(), primary_key=True),
        sa.Column("adopted_finding", sa.String(36)),
        sa.Column("adopted_at", sa.DateTime()),
        sa.Column("deferred_finding", sa.String(36)),
    )


def downgrade():
    for name in ("course_dispositions", "course_versions", "course_findings", "course_review_plans"):
        op.drop_table(name)
    op.drop_column("model_uses", "phase")
    op.drop_column("generation_steps", "publication_pending")
    op.drop_column("generation_steps", "manual_review")
    op.drop_column("generation_steps", "finding_id")

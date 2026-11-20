import sqlalchemy as sa
from alembic import op


revision = "0005_note_versions"
down_revision = "0004_project_views"


def note_lesson(*, primary_key=False):
    return sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"),
                     nullable=False, primary_key=primary_key)


def upgrade():
    op.create_table(
        "note_versions", note_lesson(primary_key=True),
        sa.Column("revision", sa.String(64), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("seen_on_disk", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "note_conflicts", sa.Column("id", sa.String(36), primary_key=True), note_lesson(),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("base_revision", sa.String(64)),
        sa.Column("external_revision", sa.String(64)),
        sa.Column("draft_revision", sa.String(64), nullable=False),
        sa.Column("resolution", sa.String()),
    )
    op.create_index("ix_note_conflicts_lesson_id", "note_conflicts", ["lesson_id"])
    op.create_table(
        "note_writes", sa.Column("id", sa.String(36), primary_key=True), note_lesson(),
        sa.Column("expected_revision", sa.String(64)),
        sa.Column("desired_revision", sa.String(64), nullable=False),
        sa.Column("resolution_id", sa.String(36)),
        sa.Column("resolution", sa.String()),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("result_conflict_id", sa.String(36)),
        sa.Column("captured_revision", sa.String(64)),
        sa.Column("captured_signature", sa.String()),
    )
    op.create_index("ix_note_writes_lesson_id", "note_writes", ["lesson_id"])


def downgrade():
    for table in ("note_writes", "note_conflicts", "note_versions"):
        op.drop_table(table)

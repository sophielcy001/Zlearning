from alembic import op
import sqlalchemy as sa


revision = "0034_assessment_notes"
down_revision = "0033_note_checks"


def upgrade():
    op.add_column("assessment_plans", sa.Column("note_scope", sa.JSON()))


def downgrade():
    op.drop_column("assessment_plans", "note_scope")

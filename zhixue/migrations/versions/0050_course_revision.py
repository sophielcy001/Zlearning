from alembic import op
import sqlalchemy as sa


revision = "0050_course_revision"
down_revision = "0049_money_budget"


def upgrade():
    op.add_column("generation_steps", sa.Column("revision_request", sa.JSON()))


def downgrade():
    op.drop_column("generation_steps", "revision_request")

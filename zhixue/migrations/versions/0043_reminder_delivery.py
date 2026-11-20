import sqlalchemy as sa
from alembic import op


revision = "0043_reminder_delivery"
down_revision = "0042_mobile_timers"


def upgrade():
    op.add_column("reminder_receipts", sa.Column("delivery_key", sa.String(36)))


def downgrade():
    op.drop_column("reminder_receipts", "delivery_key")

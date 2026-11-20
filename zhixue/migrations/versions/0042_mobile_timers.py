import sqlalchemy as sa
from alembic import op


revision = "0042_mobile_timers"
down_revision = "0041_mobile_offline"


def upgrade():
    op.add_column("timers", sa.Column("device", sa.String(), nullable=False, server_default="desktop"))
    op.add_column("timers", sa.Column("interrupted_at", sa.DateTime()))
    op.create_index("ix_timer_spans_interval", "timer_spans", ["ended_at", "began_at"])


def downgrade():
    op.drop_index("ix_timer_spans_interval", table_name="timer_spans")
    op.drop_column("timers", "interrupted_at")
    op.drop_column("timers", "device")

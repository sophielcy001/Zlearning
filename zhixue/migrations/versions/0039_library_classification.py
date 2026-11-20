from alembic import op
import sqlalchemy as sa


revision = "0039_library_classification"
down_revision = "0038_library_organization"


def upgrade():
    op.create_table(
        "library_classifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("connection", sa.JSON(), nullable=False),
        sa.Column("scope", sa.JSON()),
        sa.Column("auto_apply", sa.Boolean(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("daily_blocked_on", sa.Date()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("billing_day", sa.Date()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
    )
    op.create_index("ix_library_classifications_queue", "library_classifications", ["state", "created_at"])
    op.create_index("ix_library_classifications_billing_day", "library_classifications", ["billing_day"])
    op.create_index("ix_library_classifications_finished_at", "library_classifications", ["finished_at"])
    op.create_table(
        "library_classification_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("library_classifications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("suggestion", sa.JSON(), nullable=False),
        sa.Column("with_excerpts", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("approved", sa.JSON()),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime()),
    )
    op.create_index("ix_library_classification_candidates_queue", "library_classification_candidates", ["state", "created_at"])
    op.create_index("ix_library_classification_candidates_request_id", "library_classification_candidates", ["request_id"])


def downgrade():
    op.drop_table("library_classification_candidates")
    op.drop_table("library_classifications")

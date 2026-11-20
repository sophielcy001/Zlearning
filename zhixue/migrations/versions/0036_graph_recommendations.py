from alembic import op
import sqlalchemy as sa


revision = "0036_graph_recommendations"
down_revision = "0035_library_graph"


def upgrade():
    op.create_table(
        "graph_recommendations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("connection", sa.JSON(), nullable=False),
        sa.Column("scope", sa.JSON()),
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
    op.create_index("ix_graph_recommendations_queue", "graph_recommendations", ["state", "created_at"])
    op.create_index("ix_graph_recommendations_billing_day", "graph_recommendations", ["billing_day"])
    op.create_index("ix_graph_recommendations_finished_at", "graph_recommendations", ["finished_at"])
    op.create_table(
        "graph_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("graph_recommendations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("identity", sa.String(64), nullable=False),
        sa.Column("presentation", sa.JSON(), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("with_excerpts", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime()),
    )
    op.create_index("ix_graph_candidates_identity", "graph_candidates", ["identity"])
    op.create_index("ix_graph_candidates_queue", "graph_candidates", ["state", "created_at"])


def downgrade():
    op.drop_table("graph_candidates")
    op.drop_table("graph_recommendations")

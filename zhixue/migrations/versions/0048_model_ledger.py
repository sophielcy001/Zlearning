from alembic import op
import sqlalchemy as sa


revision = "0048_model_ledger"
down_revision = "0047_goal_authoring"


# These are the call records present at 0047. A sent or uncertain call has no
# historic price snapshot, so migration carries forward only the known usage.
HISTORY = (
    ("model_uses",
     "CASE WHEN u.assessment_job_id IS NOT NULL THEN 'assessment' WHEN u.question_id IS NOT NULL THEN 'answer' "
     "WHEN u.consolidation_id IS NOT NULL THEN 'consolidation' ELSE 'generation' END",
     "coalesce(u.assessment_job_id, u.question_id, u.consolidation_id, u.project_id)", "u.phase", "u.connection", ""),
    # A manual retry may replace the reply's recipient. The individual old
    # calls have no recipient snapshot to backfill reliably.
    ("chat_model_calls", "'chat'", "u.reply_id", "NULL", "NULL", ""),
    ("prerequisite_calls", "'prerequisite'", "u.task_id", "u.phase", "u.connection", ""),
    ("stage_review_calls", "'stage_review'", "u.review_id", "NULL", "u.connection", ""),
    ("note_suggestions", "'note_ai'", "u.id", "u.operation", "u.connection", ""),
    ("note_check_batches", "'note_check'", "u.plan_id", "NULL", "p.recipient",
     "LEFT JOIN note_check_plans p ON p.id = u.plan_id"),
    ("graph_recommendations", "'graph'", "u.id", "NULL", "u.connection", ""),
    ("library_classifications", "'classification'", "u.id", "NULL", "u.connection", ""),
    ("goal_authoring_jobs", "'goal_authoring'", "u.id", "NULL", "u.connection", ""),
)


def upgrade():
    op.add_column("model_connections", sa.Column("pricing", sa.JSON()))
    op.create_table(
        "model_charges",
        sa.Column("source", sa.String(), primary_key=True),
        sa.Column("call_id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(36), nullable=False),
        sa.Column("phase", sa.String()),
        sa.Column("recipient", sa.JSON()),
        sa.Column("billing_day", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("thinking_tokens", sa.Integer()),
        sa.Column("pricing", sa.JSON()),
        sa.Column("actual_currency", sa.String(3)),
        sa.Column("actual_amount", sa.String()),
        sa.Column("estimated_amount", sa.String()),
    )
    for table, kind, scope, phase, recipient, join in HISTORY:
        reference = "NULL"
        if recipient != "NULL":
            fields = ", ".join(f"'{key}', json_extract({recipient}, '$.{key}')" for key in ("id", "revision", "name", "provider", "model"))
            reference = (f"CASE WHEN json_valid({recipient}) THEN "
                         f"CASE WHEN json_type({recipient}) = 'object' THEN json_object({fields}) END END")
        op.execute(sa.text(f"""
            INSERT INTO model_charges (
                source, call_id, kind, scope_id, phase, recipient, billing_day,
                started_at, finished_at, status, input_tokens, output_tokens
            )
            SELECT '{table}', u.id, {kind}, {scope}, {phase}, {reference}, u.billing_day, u.started_at,
                coalesce(u.finished_at, u.started_at),
                CASE WHEN u.finished_at IS NULL THEN 'uncertain' ELSE u.status END, u.input_tokens, u.output_tokens
            FROM {table} u {join}
            WHERE u.started_at IS NOT NULL AND u.billing_day IS NOT NULL AND u.status NOT IN ('not_sent', 'reserved')
        """))
    op.create_index("ix_model_charges_day", "model_charges", ["billing_day", "started_at"])
    op.create_index("ix_model_charges_scope", "model_charges", ["kind", "scope_id"])


def downgrade():
    op.drop_table("model_charges")
    op.drop_column("model_connections", "pricing")

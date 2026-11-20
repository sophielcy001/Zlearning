from alembic import op
import sqlalchemy as sa


revision = "0049_money_budget"
down_revision = "0048_model_ledger"


def upgrade():
    op.add_column("generation_settings", sa.Column("daily_cost_limit", sa.JSON()))
    op.add_column("generation_settings", sa.Column("default_task_cost_limit", sa.JSON()))
    op.add_column("model_charges", sa.Column("reserved_amount", sa.String()))
    op.add_column("note_suggestions", sa.Column("daily_blocked_on", sa.Date()))
    op.add_column("note_check_plans", sa.Column("budget_reason", sa.String()))
    op.add_column("note_check_plans", sa.Column("budget_scope_id", sa.String(36)))
    op.execute("UPDATE note_check_plans SET budget_scope_id = id")
    op.execute("""
        UPDATE model_charges
        SET scope_id = (SELECT coalesce(job.remediation_id, job.round_id) FROM assessment_jobs job
                        WHERE job.id = model_charges.scope_id)
        WHERE kind = 'assessment' AND EXISTS (
            SELECT 1 FROM assessment_jobs job WHERE job.id = model_charges.scope_id
                AND (job.remediation_id IS NOT NULL OR job.round_id IS NOT NULL)
        )
    """)
    op.create_table(
        "model_task_budgets",
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("scope_id", sa.String(36), primary_key=True),
        sa.Column("limit_override", sa.JSON()),
        sa.Column("held_request", sa.JSON()),
    )


def downgrade():
    op.drop_table("model_task_budgets")
    op.drop_column("note_check_plans", "budget_scope_id")
    op.drop_column("note_check_plans", "budget_reason")
    op.drop_column("note_suggestions", "daily_blocked_on")
    op.drop_column("model_charges", "reserved_amount")
    op.drop_column("generation_settings", "default_task_cost_limit")
    op.drop_column("generation_settings", "daily_cost_limit")

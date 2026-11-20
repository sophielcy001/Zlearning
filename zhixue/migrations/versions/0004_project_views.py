import sqlalchemy as sa
from alembic import op


revision = "0004_project_views"
down_revision = "0003_library"


def upgrade():
    op.add_column("projects", sa.Column("created_at", sa.DateTime()))
    op.add_column("projects", sa.Column("updated_at", sa.DateTime()))
    op.add_column("projects", sa.Column("last_learned_at", sa.DateTime()))
    op.add_column("projects", sa.Column("priority", sa.String()))
    op.create_table(
        "project_tags",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
    )
    op.create_index("ix_project_tags_key", "project_tags", ["key"])
    op.create_table(
        "project_views",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("definition", sa.JSON(), nullable=False),
    )
    # Historical lessons and attempts record local study days. Timer timestamps are UTC.
    op.execute("""WITH activity AS (
        SELECT project_id, learned_on || ' 00:00:00.000000' AS moment FROM lessons
        UNION ALL
        SELECT l.project_id, a.submitted_on || ' 00:00:00.000000'
          FROM attempts AS a JOIN lessons AS l ON l.id = a.lesson_id
        UNION ALL
        SELECT l.project_id, strftime('%Y-%m-%d %H:%M:%f', t.updated_at, '+8 hours') || '000'
          FROM timers AS t JOIN lessons AS l ON l.id = t.lesson_id
         WHERE t.status = 'finished' AND t.elapsed > 0
    ), latest AS (
        SELECT project_id, max(moment) AS moment FROM activity GROUP BY project_id
    )
    UPDATE projects SET created_at = created_on || ' 00:00:00.000000',
        last_learned_at = (SELECT moment FROM latest WHERE project_id = projects.id)
    """)
    op.execute("UPDATE projects SET updated_at = max(created_at, coalesce(last_learned_at, created_at))")


def downgrade():
    op.drop_table("project_views")
    op.drop_table("project_tags")
    for name in ("priority", "last_learned_at", "updated_at", "created_at"):
        op.drop_column("projects", name)

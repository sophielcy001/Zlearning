from collections import defaultdict
from datetime import datetime, time

from alembic import op
import sqlalchemy as sa

from backend.project_versions import core_fields, fixture_route


revision = "0010_study_intake"
down_revision = "0009_backup_copies"


def upgrade():
    op.add_column("lessons", sa.Column("reading_order", sa.Integer()))
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="SET NULL")),
        sa.Column("creation_digest", sa.String(64), nullable=False),
        sa.Column("goal", sa.JSON(none_as_null=True)),
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])
    op.create_index("ix_conversations_lesson_id", "conversations", ["lesson_id"])
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("conversation_id", "position"),
    )
    op.create_table(
        "chat_attachments",
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("file_id", sa.String(36), sa.ForeignKey("imports.id"), primary_key=True),
        sa.Column("added_at", sa.DateTime(), nullable=False),
    )
    goals = op.create_table(
        "goal_versions",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("submission_digest", sa.String(64)),
    )
    op.create_index("ix_goal_versions_conversation_id", "goal_versions", ["conversation_id"])
    routes = op.create_table(
        "route_versions",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("goal_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "goal_version"], ["goal_versions.project_id", "goal_versions.version"],
                                ondelete="CASCADE"),
    )
    copy_existing_projects(goals, routes)


def copy_existing_projects(goals, routes):
    connection = op.get_bind()
    lessons = defaultdict(list)
    for project, identity, position in connection.execute(
        sa.text("SELECT project_id, id, position FROM lessons ORDER BY project_id, position")
    ):
        lessons[project].append((identity, position))
    projects = sa.text(
        "SELECT id, topic, outcome, foundation, sample, created_on, created_at FROM projects ORDER BY id"
    ).columns(created_on=sa.Date(), created_at=sa.DateTime())
    for batch in connection.execute(projects).mappings().partitions(200):
        goal_rows, route_rows = [], []
        for row in batch:
            moment = row["created_at"] or datetime.combine(row["created_on"], time.min)
            common = {"project_id": row["id"], "version": 1, "created_at": moment}
            goal_rows.append({**common, "definition": {"fields": core_fields(row), "evidence": []}})
            route_rows.append({**common, "goal_version": 1,
                               "definition": fixture_route(bool(row["sample"]), lessons[row["id"]])})
        connection.execute(goals.insert(), goal_rows)
        connection.execute(routes.insert(), route_rows)


def downgrade():
    op.drop_table("route_versions")
    op.drop_table("goal_versions")
    op.drop_table("chat_attachments")
    op.drop_table("chat_messages")
    op.drop_table("conversations")
    op.drop_column("lessons", "reading_order")

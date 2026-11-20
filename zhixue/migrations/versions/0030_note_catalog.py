from alembic import op
import sqlalchemy as sa


revision = "0030_note_catalog"
down_revision = "0029_web_snapshots"

NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
HISTORY = ("note_versions", "note_conflicts", "note_writes")


def change_owner(table, old_key, old_parent, new_key, new_parent):
    indexed = table != "note_versions"
    with op.batch_alter_table(table, recreate="always", naming_convention=NAMING) as batch:
        batch.drop_constraint(f"fk_{table}_{old_key}_{old_parent}", type_="foreignkey")
        if indexed:
            batch.drop_index(f"ix_{table}_{old_key}")
        batch.alter_column(old_key, new_column_name=new_key, existing_type=sa.String(36), existing_nullable=False)
        batch.create_foreign_key(f"fk_{table}_{new_key}_{new_parent}", new_parent, [old_key], ["id"], ondelete="CASCADE")
    if indexed:
        op.create_index(f"ix_{table}_{new_key}", table, [new_key])


def upgrade():
    op.create_table(
        "notes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(120)),
        sa.Column("metadata_revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "note_lessons",
        sa.Column("note_id", sa.String(36), sa.ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_note_lessons_lesson_id", "note_lessons", ["lesson_id"])

    # Reuse the old IDs so filenames, links, and saved drafts still identify the same note.
    op.execute("""
        INSERT INTO notes (id, title, metadata_revision)
        SELECT id, NULL, 1 FROM lessons WHERE id IN (
            SELECT lesson_id FROM note_versions
            UNION SELECT lesson_id FROM note_conflicts
            UNION SELECT lesson_id FROM note_writes
            UNION SELECT substr(id, 6) FROM library_locations WHERE id LIKE 'note-%'
            UNION SELECT substr(id, 6) FROM library_index_documents WHERE kind = 'note'
        )
    """)
    op.execute("INSERT INTO note_lessons (note_id, lesson_id) SELECT id, id FROM notes")
    for table in HISTORY:
        change_owner(table, "lesson_id", "lessons", "note_id", "notes")

    op.add_column("library_index_documents", sa.Column("project_ids", sa.Text(), nullable=False, server_default="[]"))
    op.execute("UPDATE library_index_documents SET project_ids = json_array(project_id) WHERE project_id IS NOT NULL")


def downgrade():
    db = op.get_bind()
    incompatible = db.execute(sa.text("""
        SELECT 1 FROM notes AS n
        WHERE n.title IS NOT NULL OR NOT EXISTS (SELECT 1 FROM lessons WHERE id = n.id)
           OR NOT EXISTS (SELECT 1 FROM note_lessons WHERE note_id = n.id AND lesson_id = n.id)
           OR EXISTS (SELECT 1 FROM note_lessons WHERE note_id = n.id AND lesson_id != n.id)
        LIMIT 1
    """)).first()
    if incompatible:
        raise RuntimeError("现有笔记目录无法用旧版结构表示。请用升级前的数据库备份恢复旧版。")
    op.drop_column("library_index_documents", "project_ids")
    for table in HISTORY:
        change_owner(table, "note_id", "notes", "lesson_id", "lessons")
    op.drop_table("note_lessons")
    op.drop_table("notes")

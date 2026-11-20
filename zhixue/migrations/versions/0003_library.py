import sqlalchemy as sa
from alembic import op


revision = "0003_library"
down_revision = "0002_timers"


def upgrade():
    op.execute("""CREATE VIRTUAL TABLE library_fts USING fts5(
        title, topic, body, content='library_passages', content_rowid='id', tokenize='trigram'
    )""")
    op.create_table(
        "library_index_documents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("revision", sa.String()),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("project_id", sa.String()),
        sa.Column("project_topic", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("message", sa.String()),
        sa.Column("page_count", sa.Integer()),
        sa.Column("unreadable_pages", sa.Text(), nullable=False),
    )
    op.create_table(
        "library_passages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_id", sa.String(), sa.ForeignKey("library_index_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("label", sa.String()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
    )
    op.create_index("ix_library_passages_file_id", "library_passages", ["file_id"])
    op.execute("""CREATE TRIGGER library_passages_insert AFTER INSERT ON library_passages BEGIN
        INSERT INTO library_fts(rowid, title, topic, body) VALUES (new.id, new.title, new.topic, new.body);
    END""")
    op.execute("""CREATE TRIGGER library_passages_delete AFTER DELETE ON library_passages BEGIN
        INSERT INTO library_fts(library_fts, rowid, title, topic, body) VALUES ('delete', old.id, old.title, old.topic, old.body);
    END""")


def downgrade():
    op.drop_table("library_passages")
    op.drop_table("library_index_documents")
    op.execute("DROP TABLE library_fts")

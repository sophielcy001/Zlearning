from unicodedata import normalize

from alembic import op
from sqlalchemy import text


revision = "0066_trash_reference_status"
down_revision = "0065_trash_wiki_paths"


def upgrade():
    connection = op.get_bind()
    entries = connection.execute(text("""
        SELECT t.file_id, t.original_path
        FROM library_locations l JOIN library_trash t ON t.id=l.trash_id AND t.file_id=l.id
        WHERE l.trash_id IS NOT NULL
    """)).all()
    aliases = [
        {"id": identity, "key": normalize("NFC", path.strip().replace("\\", "/")).casefold()}
        for identity, original in entries if original.startswith("library/")
        for path in (original, original.removeprefix("library/"))
    ]
    if aliases:
        connection.execute(text("INSERT OR IGNORE INTO library_wiki_aliases (file_id, key) VALUES (:id, :key)"), aliases)
    op.create_index("ix_library_wiki_aliases_key", "library_wiki_aliases", ["key", "file_id"])


def downgrade():
    op.drop_index("ix_library_wiki_aliases_key", table_name="library_wiki_aliases")

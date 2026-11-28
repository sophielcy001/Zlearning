from alembic import op
from sqlalchemy import text
from unicodedata import normalize


revision = "0065_trash_wiki_paths"
down_revision = "0064_ocr_history"


def upgrade():
    connection = op.get_bind()
    restored = connection.execute(text("""
        SELECT DISTINCT file_id, original_path FROM library_trash
        WHERE state='restored' AND restore_path!=original_path
    """)).all()
    aliases = [
        {"id": identity, "key": normalize("NFC", path.strip().replace("\\", "/")).casefold()}
        for identity, original in restored if original.startswith("library/")
        for path in (original, original.removeprefix("library/"))
    ]
    if aliases:
        connection.execute(text("INSERT OR IGNORE INTO library_wiki_aliases (file_id, key) VALUES (:id, :key)"), aliases)
        connection.execute(text("""INSERT OR IGNORE INTO library_wiki_names (key, file_id)
            SELECT :key, :id WHERE EXISTS (SELECT 1 FROM library_index_documents WHERE id=:id)"""), aliases)


def downgrade():
    # An earlier release can safely read the same aliases. Preserve existing and newly restored links.
    pass

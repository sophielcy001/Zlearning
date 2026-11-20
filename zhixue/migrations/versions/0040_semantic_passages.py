import sqlalchemy as sa
from alembic import op


revision = "0040_semantic_passages"
down_revision = "0039_library_classification"


def upgrade():
    op.add_column("library_passages", sa.Column("semantic_key", sa.LargeBinary()))
    op.execute("""UPDATE library_passages SET semantic_key=randomblob(32)
        WHERE position>0 AND length(trim(body, char(9, 10, 13, 32, 160, 12288)))>0""")
    op.create_index("ix_library_passages_semantic", "library_passages", ["semantic_key"])


def downgrade():
    op.drop_index("ix_library_passages_semantic", table_name="library_passages")
    op.drop_column("library_passages", "semantic_key")

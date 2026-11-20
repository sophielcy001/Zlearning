from collections.abc import Callable
from contextlib import closing, contextmanager, suppress
import logging
from pathlib import Path
import sqlite3
from threading import Event, Lock
from time import monotonic

from .library_index import MAX_HITS, SEARCH_FILTERS, document_info, keyword_search, location_info
from .library_metadata import ACTIVE, METADATA_COLUMNS, METADATA_JOIN
from .semantic_model import DIMENSIONS, MODEL_BYTES, LocalEmbeddings, install_model
from .storage_files import sqlite_file_uri


SCHEMA = f"""
    CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, preparing INTEGER NOT NULL, error TEXT
    );
    CREATE TABLE IF NOT EXISTS passages (key BLOB PRIMARY KEY) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS vectors (
        key BLOB NOT NULL REFERENCES passages(key) ON DELETE CASCADE,
        start INTEGER NOT NULL, end INTEGER NOT NULL, vector BLOB NOT NULL CHECK(length(vector)={DIMENSIONS * 4}),
        PRIMARY KEY(key, start, end)
    ) WITHOUT ROWID;
"""
SOURCE = "library_passages p JOIN library_index_documents d ON d.id=p.file_id"
COVERAGE = f"""SELECT count(*), count(s.key) FROM {SOURCE}
    LEFT JOIN semantic.passages s ON s.key=p.semantic_key WHERE p.semantic_key IS NOT NULL AND {ACTIVE}"""
MODEL_ERROR = "语义模型暂时无法准备。请检查网络或剩余空间后重试。"
INDEX_ERROR = "本地语义检索暂时不可用。请重试语义索引。"
PASSAGE_BATCH = 48
CHARACTER_BATCH = 14000
SCORE_MINIMUM = 0.82
SCORE_WINDOW = 0.045
logger = logging.getLogger(__name__)


class SemanticIndex:
    def __init__(self, folder: Path):
        self.database = folder / "zhixue.sqlite3"
        directory = folder / ".semantic"
        self.cache = directory / "e5-small-v1.sqlite3"
        self.assets = directory / "multilingual-e5-small"
        self.encoder = None
        self.encoder_lock = Lock()
        self.next_prune = 0.0

    @contextmanager
    def _catalog(self):
        # The model cache lives alongside the workspace, outside database and library backups.
        with closing(sqlite3.connect(sqlite_file_uri(self.database, mode="ro"), uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            db.create_function("unicode_fold", 1, str.casefold, deterministic=True)
            db.execute("ATTACH DATABASE ? AS semantic", (sqlite_file_uri(self.cache, mode="ro"),))
            db.execute("BEGIN")
            yield db

    @contextmanager
    def _write(self):
        with closing(sqlite3.connect(self.cache, timeout=5)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db

    def request(self) -> dict:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        with self._write() as db:
            db.execute("PRAGMA journal_mode=WAL").close()
            db.executescript(SCHEMA)
            db.execute("""INSERT INTO settings VALUES (1, 1, 1, NULL)
                ON CONFLICT(id) DO UPDATE SET generation=generation+1, preparing=1, error=NULL
                WHERE preparing=0 OR error IS NOT NULL""")
        return self.status()

    def rebuild(self) -> None:
        if not self.cache.is_file():
            return
        with self._write() as db:
            db.execute("DELETE FROM passages")
            db.execute("UPDATE settings SET generation=generation+1, error=NULL WHERE id=1")

    def status(self) -> dict:
        if not self.cache.is_file():
            return progress("disabled")
        try:
            with self._catalog() as db:
                return index_status(db)
        except (OSError, sqlite3.Error):
            return progress("failed", message=INDEX_ERROR)

    def _model(self):
        with self.encoder_lock:
            if self.encoder is None:
                self.encoder = LocalEmbeddings(self.assets)
            return self.encoder

    def step(self, stopped: Event) -> bool:
        if stopped.is_set() or not self.cache.is_file():
            return False
        generation = None
        preparing = False
        try:
            with self._catalog() as db:
                generation, preparing, error = settings(db)
                if error:
                    return False
                if not preparing:
                    batch = pending_passages(db)
                    if monotonic() >= self.next_prune:
                        self._prune(db)
            if preparing:
                install_model(self.assets, stopped)
                if stopped.is_set():
                    return False
                with self.encoder_lock:
                    self.encoder = None
                self._update(generation, preparing=False)
                return True
            if not batch:
                return False
            groups = self._model().passages([body for _, body in batch], stopped)
            if stopped.is_set():
                return False
            with self._write() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT generation FROM settings WHERE id=1").fetchone() != (generation,):
                    return False
                for (key, _), chunks in zip(batch, groups, strict=True):
                    db.execute("INSERT INTO passages(key) VALUES (?)", (key,))
                    db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?)", (
                        (key, start, end, vector.tobytes()) for start, end, vector in chunks
                    ))
            return True
        except Exception as error:
            if not stopped.is_set():
                logger.warning("Semantic index step failed (%s)", type(error).__name__)
                if generation is not None:
                    with suppress(OSError, sqlite3.Error):
                        self._update(generation, preparing=preparing, error=MODEL_ERROR if preparing else INDEX_ERROR)
            return False

    def _update(self, generation: int, *, preparing: bool, error: str | None = None) -> None:
        with self._write() as db:
            db.execute("UPDATE settings SET preparing=?, error=? WHERE id=1 AND generation=?",
                       (preparing, error, generation))

    def _prune(self, catalog) -> None:
        self.next_prune = monotonic() + 30
        stale = catalog.execute("""SELECT s.key FROM semantic.passages s
            WHERE NOT EXISTS (SELECT 1 FROM library_passages p WHERE p.semantic_key=s.key) LIMIT 192""").fetchall()
        if stale:
            with self._write() as db:
                db.executemany("DELETE FROM passages WHERE key=?", stale)

    def hybrid(self, query: str, scope: dict, fallback: Callable[[], dict]) -> dict:
        exact = None
        nearby = []
        status = progress("disabled")
        if self.cache.is_file():
            try:
                with self._catalog() as db:
                    status = index_status(db)
                    exact = keyword_search(query, scope, db.execute)
                    if query and status["state"] in ("ready", "indexing") and status["completed"]:
                        nearby = semantic_hits(db, scope, self._model().query(query))
            except Exception as error:
                logger.warning("Hybrid search failed (%s)", type(error).__name__)
                status = progress("failed", message=INDEX_ERROR)
        return {**merge_hits(fallback() if exact is None else exact, nearby), "semantic": status}


def settings(db):
    row = db.execute("SELECT generation, preparing, error FROM semantic.settings WHERE id=1").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("Missing semantic settings")
    return row


def progress(state: str, completed: int = 0, total: int = 0, message: str | None = None) -> dict:
    return {"state": state, "completed": completed, "total": total, "message": message, "modelBytes": MODEL_BYTES}


def index_status(db):
    _, preparing, error = settings(db)
    if error:
        return progress("failed", message=error)
    if preparing:
        return progress("preparing")
    total, completed = db.execute(COVERAGE).fetchone()
    return progress("ready" if total == completed else "indexing", completed, total)


def pending_passages(db):
    batch = []
    length = 0
    for row in db.execute(f"""SELECT p.semantic_key, p.body FROM {SOURCE}
            WHERE p.semantic_key IS NOT NULL AND {ACTIVE}
            AND NOT EXISTS (SELECT 1 FROM semantic.passages s WHERE s.key=p.semantic_key)
            ORDER BY p.id LIMIT ?""", (PASSAGE_BATCH,)):
        body = row["body"]
        length += len(body)
        if batch and length > CHARACTER_BATCH:
            break
        batch.append((row["semantic_key"], body))
    return batch


def semantic_hits(db, scope: dict, query_vector) -> list[dict]:
    import numpy as np

    cursor = db.execute(f"""SELECT p.id, v.start, v.end, v.vector FROM {SOURCE}
        JOIN semantic.vectors v ON v.key=p.semantic_key {METADATA_JOIN}
        WHERE {SEARCH_FILTERS}""", scope)
    candidates = {}
    for batch in iter(lambda: cursor.fetchmany(1024), []):
        vectors = np.frombuffer(b"".join(row[3] for row in batch), dtype="<f4").reshape(-1, DIMENSIONS)
        scores = vectors @ query_vector
        for index in np.flatnonzero(scores >= SCORE_MINIMUM):
            identity, start, end, _ = batch[index]
            score = float(scores[index])
            candidate = (score, -start, end)
            if identity not in candidates or candidate > candidates[identity]:
                candidates[identity] = candidate
    minimum = max((value[0] for value in candidates.values()), default=SCORE_MINIMUM) - SCORE_WINDOW
    best = sorted((identity for identity, value in candidates.items() if value[0] >= minimum),
                  key=lambda identity: (-candidates[identity][0], identity))[:MAX_HITS + 1]
    if not best:
        return []
    keys = ",".join("?" for _ in best)
    rows = {row["passage_id"]: row for row in db.execute(f"""SELECT d.*, {METADATA_COLUMNS}, p.id AS passage_id,
        p.position, p.label, p.body, p.text_revision FROM {SOURCE}
        {METADATA_JOIN} WHERE p.id IN ({keys})""", best)}
    hits = []
    for identity in best:
        row = rows[identity]
        _, offset, end = candidates[identity]
        body = row["body"]
        start = -offset
        stop = min(start + 170, end)
        snippet = ("…" if start else "") + " ".join(body[start:stop].split()) + ("…" if stop < len(body) else "")
        hits.append({
            **document_info(row),
            "location": location_info(row["position"], row["label"], row["text_revision"]) if row["position"] else None,
            "snippet": snippet,
        })
    return hits


def merge_hits(exact: dict, related: list[dict]) -> dict:
    hits = {}
    scores = {}
    for match, source, weight in (("keyword", exact["hits"], 1.5), ("semantic", related, 1)):
        for rank, hit in enumerate(source, 1):
            identity = hit["id"], hit["location"]["number"] if hit["location"] else 0
            scores[identity] = scores.get(identity, 0) + weight / (20 + rank)
            if identity in hits:
                hits[identity]["match"] = "both"
            else:
                hits[identity] = {**hit, "match": match}
    order = sorted(hits, key=scores.__getitem__, reverse=True)
    return {"hits": [hits[key] for key in order[:MAX_HITS]], "hasMore": exact["hasMore"] or len(order) > MAX_HITS}

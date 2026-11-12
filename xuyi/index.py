"""Exact-parity inverted index over retrieval chunks.

Mirrors retrieval.terms()/lexical_scores() so lexical rankings match the
brute-force scan byte for byte; scopes without a complete index fall back to
the scan with a warning. The index is additive, rebuildable state derived
from the verbatim archive: the archive is the body, the index is the brain.
Append-only ingests keep positions aligned with event order; deletions
invalidate the scope until an explicit rebuild.
"""
import math
from collections import Counter

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk_index(
    scope TEXT NOT NULL,event_id TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
    text TEXT NOT NULL,sender_id TEXT NOT NULL,role TEXT NOT NULL,origin TEXT NOT NULL,
    recorded_at TEXT NOT NULL,position INTEGER NOT NULL,
    PRIMARY KEY(scope,event_id,start));
CREATE INDEX IF NOT EXISTS chunk_index_by_position ON chunk_index(scope,position);
CREATE TABLE IF NOT EXISTS chunk_postings(
    scope TEXT NOT NULL,term TEXT NOT NULL,key INTEGER NOT NULL,tf INTEGER NOT NULL,
    PRIMARY KEY(scope,term,key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS chunk_df(
    scope TEXT NOT NULL,term TEXT NOT NULL,df INTEGER NOT NULL,
    PRIMARY KEY(scope,term)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS chunk_stats(
    scope TEXT PRIMARY KEY,chunks INTEGER NOT NULL,tokens INTEGER NOT NULL,max_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS access_log(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,query TEXT NOT NULL,
    event_ids TEXT NOT NULL,recorded_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS access_log_by_scope ON access_log(scope,seq);
"""

# A chunk is at most ~180 characters, so its term count stays far below the
# 1024 limit of the low bits of the posting key.
LENGTH_BITS = 10
LENGTH_MASK = (1 << LENGTH_BITS) - 1


HIGHLIGHT_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS highlight_lines(scope TEXT NOT NULL,seq INTEGER NOT NULL,"
    "PRIMARY KEY(scope,seq)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS highlight_stats(scope TEXT PRIMARY KEY,max_seq INTEGER NOT NULL)",
)


def _ensure_highlight(store):
    # Added after chunk_index existed: older archives get it lazily, and it stays
    # stale (full-scan fallback) until a rebuild/reindex fills it.
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='highlight_stats'").fetchone():
        for statement in HIGHLIGHT_SCHEMA:
            store.db.execute(statement)


def highlight_fresh(store, scope):
    _ensure_highlight(store)
    row = store.db.execute("SELECT max_seq FROM highlight_stats WHERE scope=?", (scope,)).fetchone()
    return bool(row and row[0] >= 0 and row[0] == _max_seq(store, scope))


def highlight_events(store, scope):
    """Only the events a highlight family could read, in archive order; None when stale."""
    import json
    if not highlight_fresh(store, scope):
        return None
    return [json.loads(row[0]) for row in store.db.execute(
        "SELECT e.payload FROM highlight_lines h JOIN events e ON e.seq=h.seq "
        "WHERE h.scope=? ORDER BY h.seq", (scope,))]


def _index_highlights(store, scope, events, rebuilt):
    from .highlights import relevant
    _ensure_highlight(store)
    chosen = [event["event_id"] for event in events if relevant(event)]
    seqs = {}
    for event_id in chosen:
        row = store.db.execute("SELECT seq FROM events WHERE scope=? AND event_id=?", (scope, event_id)).fetchone()
        if row:
            seqs[event_id] = row[0]
    newest = _max_seq(store, scope)
    if not rebuilt:
        # Append-only: only stay fresh if nothing was missed before this batch.
        first = min((row[0] for row in (store.db.execute(
            "SELECT seq FROM events WHERE scope=? AND event_id=?", (scope, event["event_id"])).fetchone()
            for event in events[:1]) if row), default=None)
        stats = store.db.execute("SELECT max_seq FROM highlight_stats WHERE scope=?", (scope,)).fetchone()
        before = store.db.execute("SELECT MAX(seq) FROM events WHERE scope=? AND seq<?",
                                  (scope, first if first is not None else newest + 1)).fetchone()[0] or 0
        if (stats[0] if stats else 0) != before:  # 无统计行且此前无消息 = 干净起点
            return
    store.db.executemany("INSERT OR IGNORE INTO highlight_lines(scope,seq) VALUES(?,?)",
                         [(scope, seq) for seq in seqs.values()])
    store.db.execute("INSERT INTO highlight_stats(scope,max_seq) VALUES(?,?) "
                     "ON CONFLICT(scope) DO UPDATE SET max_seq=excluded.max_seq", (scope, newest))


def _present(store):
    return bool(store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='chunk_stats'").fetchone())


def _ensure(store):
    # executescript would commit an open transaction, so only run it when the
    # index tables are genuinely absent and no restore/ingest is in flight.
    if not _present(store):
        store.db.executescript(SCHEMA)


class _Adopt:
    """Reuse the caller's transaction when one is open; else commit on exit."""

    def __init__(self, store):
        self.db = store.db

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not self.db.in_transaction:
            self.db.commit()


def _write(store):
    if store.db.in_transaction:
        return _Adopt(store)
    return store.db


def _encode(position, length):
    if length >= LENGTH_MASK:
        raise ValueError("块词元数超出索引编码范围")
    return position << LENGTH_BITS | length


def _decode(key):
    return key >> LENGTH_BITS, key & LENGTH_MASK


def _max_seq(store, scope):
    row = store.db.execute(
        "SELECT seq FROM events WHERE scope=? ORDER BY seq DESC LIMIT 1", (scope,)).fetchone()
    return row[0] if row else 0


def fresh(store, scope):
    _ensure(store)
    row = store.db.execute(
        "SELECT max_seq FROM chunk_stats WHERE scope=?", (scope,)).fetchone()
    return bool(row and row[0] >= 0 and row[0] == _max_seq(store, scope))


def chunk_count(store, scope):
    _ensure(store)
    row = store.db.execute("SELECT chunks FROM chunk_stats WHERE scope=?", (scope,)).fetchone()
    return row[0] if row else None


def invalidate(store, scope):
    """Mark the scope stale (deletions or out-of-band changes); fall back to scan."""
    if not _present(store):
        return
    with _write(store):
        store.db.execute(
            "INSERT INTO chunk_stats(scope,chunks,tokens,max_seq) VALUES(?,0,0,-1) "
            "ON CONFLICT(scope) DO UPDATE SET max_seq=-1", (scope,))
        _ensure_highlight(store)
        store.db.execute(
            "INSERT INTO highlight_stats(scope,max_seq) VALUES(?,-1) "
            "ON CONFLICT(scope) DO UPDATE SET max_seq=-1", (scope,))


def forget(store, scope, event_id):
    if not _present(store):
        return
    with _write(store):
        rows = store.db.execute(
            "DELETE FROM chunk_index WHERE scope=? AND event_id=? RETURNING position",
            (scope, event_id)).fetchall()
        for (position,) in rows:
            store.db.execute("DELETE FROM chunk_postings WHERE scope=? AND key BETWEEN ? AND ?",
                             (scope, position << LENGTH_BITS, (position << LENGTH_BITS) | LENGTH_MASK))
    invalidate(store, scope)


def rebuild(store, scope):
    """Rebuild the index for one scope from the verbatim archive."""
    _ensure(store)
    events = store.events(scope)
    with _write(store):
        store.db.execute("DELETE FROM chunk_postings WHERE scope=?", (scope,))
        store.db.execute("DELETE FROM chunk_df WHERE scope=?", (scope,))
        store.db.execute("DELETE FROM chunk_index WHERE scope=?", (scope,))
        store.db.execute("DELETE FROM chunk_stats WHERE scope=?", (scope,))
        _ensure_highlight(store)
        store.db.execute("DELETE FROM highlight_lines WHERE scope=?", (scope,))
        store.db.execute("DELETE FROM highlight_stats WHERE scope=?", (scope,))
    _insert(store, scope, events)
    with _write(store):
        _index_highlights(store, scope, events, rebuilt=True)


def _insert(store, scope, events):
    from .retrieval import chunks, terms
    position = store.db.execute(
        "SELECT COUNT(*) FROM chunk_index WHERE scope=?", (scope,)).fetchone()[0]
    chunk_rows, posting_rows, df_deltas = [], [], Counter()
    tokens_added = 0
    for event in events:
        for piece in chunks([event]):
            counts = Counter(terms(piece["text"]))
            length = sum(counts.values())
            tokens_added += length
            chunk_rows.append((scope, piece["event_id"], piece["start"], piece["end"],
                               piece["text"], piece["sender_id"], piece["role"], piece["origin"],
                               piece["recorded_at"], position))
            for term in sorted(counts):
                posting_rows.append((scope, term, _encode(position, length), counts[term]))
                df_deltas[term] += 1
            position += 1
    with _write(store):
        store.db.executemany(
            "INSERT OR IGNORE INTO chunk_index(scope,event_id,start,end,text,sender_id,role,"
            "origin,recorded_at,position) VALUES(?,?,?,?,?,?,?,?,?,?)", chunk_rows)
        store.db.executemany(
            "INSERT OR IGNORE INTO chunk_postings(scope,term,key,tf) VALUES(?,?,?,?)", posting_rows)
        _bump_df(store, scope, df_deltas)
        store.db.execute(
            "INSERT INTO chunk_stats(scope,chunks,tokens,max_seq) VALUES(?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET chunks=chunks+excluded.chunks,"
            "tokens=tokens+excluded.tokens,max_seq=excluded.max_seq",
            (scope, len(chunk_rows), tokens_added, _max_seq(store, scope)))


def _bump_df(store, scope, deltas):
    """Maintain document frequencies; callers guarantee each delta is a fresh chunk."""
    store.db.executemany(
        "INSERT INTO chunk_df(scope,term,df) VALUES(?,?,?) "
        "ON CONFLICT(scope,term) DO UPDATE SET df=df+excluded.df",
        [(scope, term, delta) for term, delta in sorted(deltas.items())])


def update(store, scope, events):
    """Index newly archived events of one scope; callers pass them in archive order."""
    _ensure(store)
    known = {row[0] for row in store.db.execute(
        "SELECT DISTINCT event_id FROM chunk_index WHERE scope=?", (scope,))}
    fresh_events = [event for event in events if event["event_id"] not in known]
    if fresh_events:
        _insert(store, scope, fresh_events)
        with _write(store):
            _index_highlights(store, scope, fresh_events, rebuilt=False)


def ranked_positions(store, scope, query):
    """Positive-score chunk positions ordered exactly like retrieval.order()."""
    from .retrieval import terms
    _ensure(store)
    stats = store.db.execute(
        "SELECT chunks,tokens FROM chunk_stats WHERE scope=?", (scope,)).fetchone()
    if not stats or not stats[0]:
        return []
    n, tokens = stats
    avg = tokens / n or 1
    scores = {}
    for term in sorted(set(terms(query))):
        df = store.db.execute(
            "SELECT df FROM chunk_df WHERE scope=? AND term=?", (scope, term)).fetchone()
        df = df[0] if df else 0
        if not df:
            continue
        idf = math.log1p((n - df + 0.5) / (df + 0.5))
        for key, tf in store.db.execute(
                "SELECT key,tf FROM chunk_postings WHERE scope=? AND term=?", (scope, term)):
            position, length = _decode(key)
            scores[position] = scores.get(position, 0.0) + \
                idf * 2.2 * tf / (tf + 1.2 * (0.25 + 0.75 * length / avg))
    return [position for position, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            if score > 0]


def document_frequency(store, scope, term):
    row = store.db.execute(
        "SELECT df FROM chunk_df WHERE scope=? AND term=?", (scope, term)).fetchone()
    return row[0] if row else 0


def ranked_pieces(store, scope, query, limit):
    """Piece dicts for the top-ranked positions, in ranked order."""
    positions = ranked_positions(store, scope, query)[:limit]
    if not positions:
        return []
    found = {}
    for position in positions:
        row = store.db.execute(
            "SELECT event_id,start,end,text,sender_id,role,origin,recorded_at "
            "FROM chunk_index WHERE scope=? AND position=?", (scope, position)).fetchone()
        if row:
            found[position] = {"event_id": row[0], "start": row[1], "end": row[2], "text": row[3],
                               "sender_id": row[4], "role": row[5], "origin": row[6],
                               "recorded_at": row[7]}
    return [found[position] for position in positions if position in found]

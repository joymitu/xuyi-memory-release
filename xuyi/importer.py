"""Resumable, idempotent history import with a per-source watermark.

The archive itself is already idempotent (stable event IDs dedupe), so a
re-run can never duplicate or resurrect history; the ledger only tracks how
far each source has been consumed so rescans skip finished ranges. One bad
record never aborts a batch — it is reported and skipped instead.
"""
LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_ledger(
    source TEXT NOT NULL,scope TEXT NOT NULL,watermark TEXT NOT NULL,
    imported INTEGER NOT NULL,duplicates INTEGER NOT NULL,failed INTEGER NOT NULL,
    updated_at TEXT NOT NULL,provenance TEXT,
    PRIMARY KEY(source,scope));
"""


def _present(store):
    return bool(store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='import_ledger'").fetchone())


def _ensure(store):
    if not _present(store):
        store.db.executescript(LEDGER_SCHEMA)


def ledger(store, source):
    _ensure(store)
    return [dict(row) for row in store.db.execute(
        "SELECT scope,watermark,imported,duplicates,failed,updated_at FROM import_ledger "
        "WHERE source=? ORDER BY scope", (source,))]


def _record(store, source, scope, watermark, counts, updated_at, provenance=None):
    store.db.execute(
        "INSERT INTO import_ledger(source,scope,watermark,imported,duplicates,failed,updated_at,provenance) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source,scope) DO UPDATE SET "
        "watermark=excluded.watermark,imported=imported+excluded.imported,"
        "duplicates=duplicates+excluded.duplicates,failed=failed+excluded.failed,"
        "updated_at=excluded.updated_at,"
        "provenance=COALESCE(excluded.provenance,import_ledger.provenance)",
        (source, scope, watermark, counts["inserted"], counts["duplicate"],
         counts["failed"], updated_at, provenance))


def import_events(store, source, events, batch=500, watermark_of=None, now=None, provenance=None):
    """Import one source's events; commit per batch, advance the ledger per scope.

    provenance records the source's generation/verification conditions —
    transferred memories must carry their protocol context (bio principle:
    the C. elegans replication saga shows an effect is valid only within its
    protocol boundary).
    """
    from . import index as xuyi_index
    from .store import MemoryError, now as store_now
    _ensure(store)
    now = now or store_now
    watermark_of = watermark_of or (lambda event: str(event.get("event_id", "")))
    batch = max(int(batch), 1)
    totals = {"inserted": 0, "duplicate": 0, "deleted": 0, "failed": 0}
    failures = []
    scopes = sorted({event["scope"] for event in events})
    grouped = {}
    for event in events:
        grouped.setdefault(event["scope"], []).append(
            (event, str(watermark_of(event))))
    for scope, pairs in grouped.items():
        for start in range(0, len(pairs), batch):
            chunk = pairs[start:start + batch]
            counts = {"inserted": 0, "duplicate": 0, "deleted": 0, "failed": 0}
            inserted_events = []
            # store.add commits its own single-statement transaction; nesting it
            # inside another `with store.db` would commit the outer batch early.
            for event, mark in chunk:
                try:
                    status = store.add(event)
                except MemoryError as exc:
                    counts["failed"] += 1
                    failures.append({"scope": scope, "event_id": event.get("event_id"),
                                     "error": str(exc)})
                    continue
                counts[status] += 1
                if status == "inserted":
                    inserted_events.append(event)
            with store.db:
                if inserted_events:
                    xuyi_index.update(store, scope, inserted_events)
                watermark = max(mark for _, mark in chunk)
                _record(store, source, scope, watermark, counts, now(), provenance)
            for key in totals:
                totals[key] += counts[key]
    return {"source": source, "scopes": scopes, "totals": totals,
            "failure_count": len(failures), "failures": failures[:50]}

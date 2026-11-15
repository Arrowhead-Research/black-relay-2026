-- BlackRelay F2 schema. Owner (sole writer): the normalizer service. Outbound opens the file read-only.
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;     -- every commit is fsynced (WAL): an acknowledged event survives power loss

-- Event log. Retention prunes strictly by seq PREFIX (never a hole in the middle). AUTOINCREMENT + insert-only inside one transaction per batch gives a gap-free, strictly
-- monotonic seq (a rolled-back batch also rolls back sqlite_sequence). Retention may delete any old row, even all
-- of them: AUTOINCREMENT's sqlite_sequence keeps the counter monotonic across pruning and restarts.
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT    NOT NULL UNIQUE,          -- "evt-<seq>"
    ts         TEXT    NOT NULL,                 -- ISO-8601 UTC 'YYYY-MM-DDTHH:MM:SS.mmmZ'
    type       TEXT    NOT NULL,
    subject    TEXT    NOT NULL,
    from_json  TEXT,                             -- JSON-encoded scalar or null
    to_json    TEXT,
    source     TEXT,
    synthetic  INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0,1)),
    zone_json  TEXT                              -- zone events: {"name","kind","partition","state","tamper","battery_low","online","stale"} AS OF the event;
                                                 -- partition events: {"name","arming","alarm_type","stale"} AS OF the event
);                                               -- (older databases get the column from Store's migration)
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);   -- retention (ISO-8601 sorts chronologically)

-- Persisted published cache so a normalizer restart diffs the next snapshot against what consumers saw.
CREATE TABLE IF NOT EXISTS zone_state      (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS partition_state (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta            (k  TEXT PRIMARY KEY, v    TEXT NOT NULL);

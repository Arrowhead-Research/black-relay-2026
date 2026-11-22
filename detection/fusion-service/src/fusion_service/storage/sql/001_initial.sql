CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO metadata VALUES ('schema_version', 1), ('floor_cursor', 0);
CREATE TABLE IF NOT EXISTS events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    received REAL NOT NULL,
    site_id TEXT NOT NULL,
    subsystem TEXT NOT NULL,
    type TEXT NOT NULL,
    node_id TEXT,
    body TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_site_cursor ON events(site_id, cursor);
CREATE INDEX IF NOT EXISTS events_type_cursor ON events(type, cursor);
CREATE INDEX IF NOT EXISTS events_node_cursor ON events(node_id, cursor);
CREATE INDEX IF NOT EXISTS events_id ON events(json_extract(body, '$.id'));
CREATE TABLE IF NOT EXISTS seen (
    namespace TEXT NOT NULL,
    schema TEXT NOT NULL,
    source_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    expires REAL NOT NULL,
    PRIMARY KEY(namespace, schema, source_id)
);
CREATE INDEX IF NOT EXISTS seen_expiry ON seen(expires);
CREATE TABLE IF NOT EXISTS nodes (
    site_id TEXT NOT NULL,
    subsystem TEXT NOT NULL,
    node_id TEXT NOT NULL,
    occurred REAL NOT NULL,
    received REAL NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY(site_id, subsystem, node_id)
);

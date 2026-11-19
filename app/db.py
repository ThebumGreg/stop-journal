import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
  id INTEGER PRIMARY KEY,
  device TEXT NOT NULL,
  tst_utc INTEGER NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  acc REAL,
  raw_json TEXT,
  UNIQUE(device, tst_utc)
);
CREATE INDEX IF NOT EXISTS idx_points_time ON points(device, tst_utc);

CREATE TABLE IF NOT EXISTS days (
  date TEXT PRIMARY KEY,
  processed_at TEXT,
  params_json TEXT,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS known_sites (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  address TEXT,
  lat REAL, lon REAL,
  radius_m REAL,
  category TEXT,
  hubspot_company_id TEXT,
  times_confirmed INTEGER DEFAULT 0,
  last_confirmed TEXT,
  kind TEXT DEFAULT 'client',
  default_status TEXT DEFAULT 'Draft'
);

CREATE TABLE IF NOT EXISTS place_cache (
  key TEXT PRIMARY KEY,
  response_json TEXT NOT NULL,
  fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
  day TEXT NOT NULL,
  provider TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, provider)
);

CREATE TABLE IF NOT EXISTS parts (
  id INTEGER PRIMARY KEY,
  sku TEXT,
  name TEXT NOT NULL,
  unit TEXT DEFAULT 'ea',
  unit_cost REAL,
  billable INTEGER DEFAULT 1,
  category TEXT,
  on_hand REAL DEFAULT 0,
  reorder_at REAL,
  tracked INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_parts_sku ON parts(lower(sku))
  WHERE sku IS NOT NULL AND sku != '';

CREATE TABLE IF NOT EXISTS stop_parts (
  id INTEGER PRIMARY KEY,
  stop_id INTEGER NOT NULL,
  part_id INTEGER,
  name TEXT NOT NULL,
  sku TEXT,
  unit TEXT,
  qty REAL NOT NULL,
  unit_cost REAL,
  billable INTEGER DEFAULT 1,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_stop_parts_stop ON stop_parts(stop_id);

CREATE TABLE IF NOT EXISTS inventory_log (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  delta REAL NOT NULL,
  reason TEXT NOT NULL,
  stop_part_id INTEGER,
  note TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  title TEXT,
  message TEXT,
  ok INTEGER NOT NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS alert_state (
  key TEXT PRIMARY KEY,
  active INTEGER NOT NULL DEFAULT 0,
  last_sent INTEGER,
  last_attempt INTEGER
);

CREATE TABLE IF NOT EXISTS stops (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  seq INTEGER,
  lat REAL, lon REAL,
  arrive_utc INTEGER, depart_utc INTEGER,
  business_name TEXT, address TEXT,
  known_site_id INTEGER REFERENCES known_sites(id),
  hubspot_company_id TEXT,
  notes TEXT,
  status TEXT DEFAULT 'Draft',
  source TEXT DEFAULT 'detected',
  kind TEXT DEFAULT 'stop',
  category TEXT,
  edited INTEGER DEFAULT 0,
  times_edited INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS undo_log (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  ts INTEGER NOT NULL,
  label TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_undo_date ON undo_log(date, id);

CREATE TABLE IF NOT EXISTS day_marks (
  date TEXT PRIMARY KEY,
  start_utc INTEGER,
  end_utc INTEGER
);

CREATE TABLE IF NOT EXISTS drives (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  seq INTEGER,
  from_stop_id INTEGER, to_stop_id INTEGER,
  depart_utc INTEGER, arrive_utc INTEGER,
  distance_m REAL,
  notes TEXT,
  edited INTEGER DEFAULT 0
);
"""


# Columns added after the first release; applied to databases created earlier.
MIGRATIONS = [
    ("stops", "kind", "TEXT DEFAULT 'stop'"),
    ("stops", "category", "TEXT"),
    ("stops", "edited", "INTEGER DEFAULT 0"),
    ("stops", "times_edited", "INTEGER DEFAULT 0"),
    ("drives", "edited", "INTEGER DEFAULT 0"),
    ("known_sites", "kind", "TEXT DEFAULT 'client'"),
    ("known_sites", "default_status", "TEXT DEFAULT 'Draft'"),
]


def _migrate(conn):
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def connect(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(data_dir, "stop_journal.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn

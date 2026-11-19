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
  last_confirmed TEXT
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
  edited INTEGER DEFAULT 0
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
    ("drives", "edited", "INTEGER DEFAULT 0"),
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

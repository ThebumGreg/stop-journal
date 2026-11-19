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
  source TEXT DEFAULT 'detected'
);

CREATE TABLE IF NOT EXISTS drives (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  seq INTEGER,
  from_stop_id INTEGER, to_stop_id INTEGER,
  depart_utc INTEGER, arrive_utc INTEGER,
  distance_m REAL,
  notes TEXT
);
"""


def connect(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(data_dir, "stop_journal.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

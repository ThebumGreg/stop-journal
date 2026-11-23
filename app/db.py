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
  active INTEGER DEFAULT 1,
  kind TEXT DEFAULT 'part',
  vendor TEXT
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
  notes TEXT,
  kind TEXT DEFAULT 'part'
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

-- purchases: what was paid for stock (shopping list "Bought", or + Add stock with a price)
CREATE TABLE IF NOT EXISTS purchases (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  vendor TEXT,
  qty REAL NOT NULL,
  total REAL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchases_part ON purchases(part_id);

-- tools and equipment (Truck → Tools): what you own and what you paid; receipts/photos in tool_files
CREATE TABLE IF NOT EXISTS tools (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  make TEXT, model TEXT, serial TEXT, category TEXT,
  purchased TEXT, price REAL, vendor TEXT, warranty_until TEXT,
  location TEXT NOT NULL DEFAULT 'truck',
  notes TEXT,
  created INTEGER
);
CREATE TABLE IF NOT EXISTS tool_files (
  id INTEGER PRIMARY KEY,
  tool_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  created INTEGER
);

-- install kits: a named set of parts added to a stop in one tap
CREATE TABLE IF NOT EXISTS kits (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS kit_items (
  id INTEGER PRIMARY KEY,
  kit_id INTEGER NOT NULL,
  part_id INTEGER NOT NULL,
  qty REAL NOT NULL
);

-- vendor details (one row per vendor name; the name links parts.vendor and vendor places)
CREATE TABLE IF NOT EXISTS vendors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  account TEXT, phone TEXT, contact TEXT, website TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS follow_ups (
  id INTEGER PRIMARY KEY,
  stop_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  owner TEXT,
  due TEXT
);
CREATE INDEX IF NOT EXISTS idx_follow_ups_stop ON follow_ups(stop_id);

-- photos and receipts (app/photos.py): files under photos/YYYY/MM/DD/, the path is kept here
-- (data is only used by databases from before the move to files)
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY,
  stop_id INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'receipt',
  caption TEXT,
  created INTEGER NOT NULL,
  width INTEGER, height INTEGER,
  data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_stop ON photos(stop_id);

-- one fill-up per Gas stop: what the pump and the dashboard said
CREATE TABLE IF NOT EXISTS fuel (
  stop_id INTEGER PRIMARY KEY,
  gallons REAL,
  total REAL,
  odometer INTEGER
);

CREATE TABLE IF NOT EXISTS route_cache (
  key TEXT PRIMARY KEY,
  meters REAL NOT NULL,
  ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS undo_log (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  ts INTEGER NOT NULL,
  label TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_undo_date ON undo_log(date, id);

CREATE TABLE IF NOT EXISTS email_log (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  date_from TEXT NOT NULL, date_to TEXT NOT NULL,
  mail_to TEXT, subject TEXT,
  version INTEGER, ok INTEGER, error TEXT, attached TEXT
);

CREATE TABLE IF NOT EXISTS cal_sync (
  stop_id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  uid TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  hash TEXT,
  cancelled INTEGER DEFAULT 0,
  event_json TEXT
);

CREATE TABLE IF NOT EXISTS cal_sync_log (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL, ts INTEGER NOT NULL, stop_id INTEGER, method TEXT
);

CREATE TABLE IF NOT EXISTS trips (
  id INTEGER PRIMARY KEY,
  site_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  set_at INTEGER NOT NULL,
  stop_id INTEGER,
  cleared INTEGER DEFAULT 0
);

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
    ("stops", "work_category", "TEXT"),
    ("stops", "ticket", "TEXT"),
    ("stops", "visit_result", "TEXT"),
    ("parts", "kind", "TEXT DEFAULT 'part'"),
    ("parts", "vendor", "TEXT"),
    ("parts", "pack_size", "REAL"),
    ("parts", "pack_unit", "TEXT"),
    ("stop_parts", "kind", "TEXT DEFAULT 'part'"),
    ("stops", "edited", "INTEGER DEFAULT 0"),
    ("stops", "times_edited", "INTEGER DEFAULT 0"),
    ("stops", "arrive_set", "INTEGER DEFAULT 0"),
    ("stops", "depart_set", "INTEGER DEFAULT 0"),
    ("drives", "edited", "INTEGER DEFAULT 0"),
    ("known_sites", "kind", "TEXT DEFAULT 'client'"),
    ("known_sites", "default_status", "TEXT DEFAULT 'Draft'"),
    ("stops", "stop_type", "TEXT"),
    ("photos", "path", "TEXT"),
    ("photos", "read_json", "TEXT"),
    ("route_cache", "seconds", "REAL"),
    ("parts", "bin", "TEXT"),
    ("known_sites", "link_id", "INTEGER"),
    ("known_sites", "old_address", "INTEGER"),
    ("parts", "shelf", "TEXT"),
    ("parts", "last_cost", "REAL"),
    ("stops", "fuel_check", "INTEGER"),
    ("tool_files", "read_json", "TEXT"),
    ("stops", "uid", "TEXT"),
    ("stops", "qbo_estimate", "TEXT"),
    ("parts", "vendor_item", "TEXT"),
    ("parts", "qbo_item", "TEXT"),
    ("stops", "ticket_action", "TEXT"),
]


# A random UUID (version 4 form) in SQL, so every way a stop is created gets one (see the trigger below).
UUID_SQL = ("lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))), 2) "
            "|| '-' || substr('89ab', 1 + abs(random()) % 4, 1) || substr(lower(hex(randomblob(2))), 2) || '-' "
            "|| lower(hex(randomblob(6)))")


def _migrate(conn):
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # stops.uid: a stable ID for the report (stop numbers shift when stops are added, merged or split). Filled by a
    # trigger on every insert that has none (Undo re-inserts rows WITH their uid, so it is restored); existing rows once.
    conn.execute(f"UPDATE stops SET uid={UUID_SQL} WHERE uid IS NULL")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS stops_uid AFTER INSERT ON stops WHEN NEW.uid IS NULL "
                 f"BEGIN UPDATE stops SET uid={UUID_SQL} WHERE id=NEW.id; END")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_stops_uid ON stops(uid)")
    conn.commit()


def connect(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(data_dir, "stop_journal.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    from . import photos             # pictures stored in the database by the first version move to files
    photos.move_to_files(conn, data_dir)
    from .places import known       # one-time data repair: vendor branches saved as clients
    known.repair_vendor_branches(conn)
    from . import parts as parts_mod  # one-time: the first "Shelf / bin" field splits into Shelf and Bin
    parts_mod.split_shelf_bin(conn)
    return conn

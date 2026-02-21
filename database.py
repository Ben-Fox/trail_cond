import sqlite3
import os
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), 'trailcondish.db')

# Thread-local connection pool
_local = threading.local()

def get_db():
    """Get a thread-local database connection (reused within same thread)."""
    if not hasattr(_local, 'conn') or _local.conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
        conn.execute("PRAGMA busy_timeout=5000")  # 5s wait on lock
        _local.conn = conn
    return _local.conn

def close_db():
    """Close the thread-local connection."""
    if hasattr(_local, 'conn') and _local.conn:
        _local.conn.close()
        _local.conn = None

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility_id TEXT NOT NULL,
            trail_name TEXT,
            trail_condition TEXT,
            trail_surface TEXT,
            weather TEXT,
            road_access TEXT,
            parking TEXT,
            issues TEXT,
            general_notes TEXT,
            date_visited TEXT,
            upvotes INTEGER DEFAULT 0,
            downvotes INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            ip_hash TEXT NOT NULL,
            vote_type TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(report_id, ip_hash)
        );
        CREATE TABLE IF NOT EXISTS quick_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_type TEXT NOT NULL,
            facility_id TEXT,
            trail_name TEXT,
            lat REAL,
            lon REAL,
            category TEXT NOT NULL,
            detail TEXT,
            notes TEXT,
            upvotes INTEGER DEFAULT 0,
            downvotes INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_reports_facility ON reports(facility_id);
        CREATE INDEX IF NOT EXISTS idx_votes_report ON votes(report_id);
        CREATE INDEX IF NOT EXISTS idx_quicklogs_facility ON quick_logs(facility_id);
        CREATE INDEX IF NOT EXISTS idx_quicklogs_location ON quick_logs(lat, lon);
    ''')
    # Add new columns if they don't exist (migration)
    try:
        conn.execute('ALTER TABLE reports ADD COLUMN trail_name TEXT')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE reports ADD COLUMN weather TEXT')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE reports ADD COLUMN parking TEXT')
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE reports ADD COLUMN issues TEXT')
    except Exception:
        pass

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'trailcondish.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility_id TEXT NOT NULL,
            facility_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Good',
            trail_condition TEXT DEFAULT '',
            road_access TEXT DEFAULT '',
            general_notes TEXT DEFAULT '',
            date_visited TEXT DEFAULT '',
            upvotes INTEGER DEFAULT 0,
            downvotes INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS standalone_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility_id TEXT DEFAULT '',
            facility_name TEXT NOT NULL,
            state TEXT DEFAULT '',
            area_region TEXT DEFAULT '',
            date_visited TEXT DEFAULT '',
            overall_condition TEXT DEFAULT '',
            cleanliness INTEGER DEFAULT 0,
            trail_surface TEXT DEFAULT '',
            road_access TEXT DEFAULT '',
            crowding TEXT DEFAULT '',
            water_availability TEXT DEFAULT '',
            restroom_condition TEXT DEFAULT '',
            general_notes TEXT DEFAULT '',
            hazards TEXT DEFAULT '',
            recommend TEXT DEFAULT '',
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
    ''')
    conn.close()

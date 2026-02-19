import sqlite3
import os
from datetime import datetime

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
            facility_name TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            trail_condition TEXT DEFAULT '',
            trash_level TEXT DEFAULT '',
            accessibility_notes TEXT DEFAULT '',
            general_notes TEXT DEFAULT '',
            date_visited TEXT DEFAULT '',
            photo_url TEXT DEFAULT '',
            upvotes INTEGER DEFAULT 0,
            downvotes INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_reports_facility ON reports(facility_id);
        
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            vote_type TEXT NOT NULL,
            ip_hash TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (report_id) REFERENCES reports(id)
        );
        CREATE INDEX IF NOT EXISTS idx_votes_report ON votes(report_id);
    ''')
    conn.close()

def add_report(facility_id, data):
    conn = get_db()
    cur = conn.execute('''
        INSERT INTO reports (facility_id, facility_name, status, trail_condition, trash_level,
            accessibility_notes, general_notes, date_visited, photo_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        facility_id,
        data.get('facility_name', ''),
        data.get('status', 'open'),
        data.get('trail_condition', ''),
        data.get('trash_level', ''),
        data.get('accessibility_notes', ''),
        data.get('general_notes', ''),
        data.get('date_visited', ''),
        data.get('photo_url', '')
    ))
    conn.commit()
    report_id = cur.lastrowid
    conn.close()
    return report_id

def get_reports(facility_id, limit=50):
    conn = get_db()
    rows = conn.execute('''
        SELECT * FROM reports WHERE facility_id = ? ORDER BY created_at DESC LIMIT ?
    ''', (facility_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def vote_report(report_id, vote_type, ip_hash=''):
    conn = get_db()
    # Check for existing vote from same IP
    existing = conn.execute(
        'SELECT id, vote_type FROM votes WHERE report_id = ? AND ip_hash = ?',
        (report_id, ip_hash)
    ).fetchone()
    
    if existing:
        if existing['vote_type'] == vote_type:
            conn.close()
            return False  # Already voted same way
        # Change vote
        conn.execute('UPDATE votes SET vote_type = ? WHERE id = ?', (vote_type, existing['id']))
        if vote_type == 'up':
            conn.execute('UPDATE reports SET upvotes = upvotes + 1, downvotes = downvotes - 1 WHERE id = ?', (report_id,))
        else:
            conn.execute('UPDATE reports SET upvotes = upvotes - 1, downvotes = downvotes + 1 WHERE id = ?', (report_id,))
    else:
        conn.execute('INSERT INTO votes (report_id, vote_type, ip_hash) VALUES (?, ?, ?)',
                     (report_id, vote_type, ip_hash))
        if vote_type == 'up':
            conn.execute('UPDATE reports SET upvotes = upvotes + 1 WHERE id = ?', (report_id,))
        else:
            conn.execute('UPDATE reports SET downvotes = downvotes + 1 WHERE id = ?', (report_id,))
    
    conn.commit()
    report = dict(conn.execute('SELECT upvotes, downvotes FROM reports WHERE id = ?', (report_id,)).fetchone())
    conn.close()
    return report

def get_condition_summary(facility_id):
    conn = get_db()
    reports = conn.execute('''
        SELECT status, trail_condition, trash_level, date_visited, created_at
        FROM reports WHERE facility_id = ? ORDER BY created_at DESC LIMIT 10
    ''', (facility_id,)).fetchall()
    conn.close()
    
    if not reports:
        return None
    
    reports = [dict(r) for r in reports]
    
    # Most common status
    statuses = [r['status'] for r in reports if r['status']]
    conditions = [r['trail_condition'] for r in reports if r['trail_condition']]
    trash = [r['trash_level'] for r in reports if r['trash_level']]
    
    def most_common(lst):
        if not lst: return None
        return max(set(lst), key=lst.count)
    
    return {
        'report_count': len(reports),
        'last_reported': reports[0]['created_at'],
        'status': most_common(statuses) or 'unknown',
        'trail_condition': most_common(conditions) or 'unknown',
        'trash_level': most_common(trash) or 'unknown',
    }

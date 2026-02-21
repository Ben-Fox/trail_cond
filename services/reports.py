import hashlib
from flask import Blueprint, request, jsonify
from database import get_db

reports_bp = Blueprint('reports', __name__)


@reports_bp.route('/api/trail/<path:facility_id>/reports')
def get_reports(facility_id):
    db = get_db()
    reports = db.execute('SELECT * FROM reports WHERE facility_id=? ORDER BY created_at DESC', (facility_id,)).fetchall()
    return jsonify([dict(r) for r in reports])


@reports_bp.route('/api/trail/<path:facility_id>/reports', methods=['POST'])
def add_report(facility_id):
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    db = get_db()
    try:
        db.execute('''INSERT INTO reports (facility_id, trail_name, trail_condition, trail_surface, weather, road_access, parking, issues, general_notes, date_visited) 
                      VALUES (?,?,?,?,?,?,?,?,?,?)''',
                   (facility_id, data.get('trail_name'), data.get('trail_condition'), data.get('trail_surface'),
                    data.get('weather'), data.get('road_access'), data.get('parking'), data.get('issues'),
                    data.get('general_notes'), data.get('date_visited')))
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reports_bp.route('/api/reports/<int:report_id>/vote', methods=['POST'])
def vote_report(report_id):
    data = request.json
    vote_type = data.get('vote_type', 'up')
    remote = request.headers.get('X-Forwarded-For', request.remote_addr or '0.0.0.0').split(',')[0].strip()
    ip_hash = hashlib.sha256(remote.encode()).hexdigest()[:16]
    db = get_db()
    try:
        existing = db.execute('SELECT vote_type FROM votes WHERE report_id=? AND ip_hash=?', (report_id, ip_hash)).fetchone()
        if existing:
            if existing['vote_type'] == vote_type:
                return jsonify({'error': 'Already voted'}), 400
            db.execute('UPDATE votes SET vote_type=? WHERE report_id=? AND ip_hash=?', (vote_type, report_id, ip_hash))
            if vote_type == 'up':
                db.execute('UPDATE reports SET upvotes=upvotes+1, downvotes=downvotes-1 WHERE id=?', (report_id,))
            else:
                db.execute('UPDATE reports SET downvotes=downvotes+1, upvotes=upvotes-1 WHERE id=?', (report_id,))
        else:
            db.execute('INSERT INTO votes (report_id, ip_hash, vote_type) VALUES (?,?,?)', (report_id, ip_hash, vote_type))
            if vote_type == 'up':
                db.execute('UPDATE reports SET upvotes=upvotes+1 WHERE id=?', (report_id,))
            else:
                db.execute('UPDATE reports SET downvotes=downvotes+1 WHERE id=?', (report_id,))
        db.commit()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@reports_bp.route('/api/quicklog', methods=['POST'])
def add_quicklog():
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    if not data.get('category'):
        return jsonify({'error': 'Category required'}), 400
    db = get_db()
    try:
        db.execute('''INSERT INTO quick_logs (log_type, facility_id, trail_name, lat, lon, category, detail, notes)
                      VALUES (?,?,?,?,?,?,?,?)''',
                   (data.get('log_type', 'trail'), data.get('facility_id'), data.get('trail_name'),
                    data.get('lat'), data.get('lon'), data.get('category'),
                    data.get('detail'), data.get('notes')))
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reports_bp.route('/api/quicklogs/<path:facility_id>')
def get_quicklogs(facility_id):
    db = get_db()
    logs = db.execute('SELECT * FROM quick_logs WHERE facility_id=? ORDER BY created_at DESC LIMIT 50', (facility_id,)).fetchall()
    return jsonify([dict(r) for r in logs])


@reports_bp.route('/api/quicklogs/nearby')
def get_nearby_quicklogs():
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    radius = min(request.args.get('radius', 0.1, type=float), 1.0)  # Cap at ~70mi
    if lat is None or lon is None:
        return jsonify({'error': 'lat/lon required'}), 400
    db = get_db()
    logs = db.execute('''SELECT * FROM quick_logs 
                         WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
                         ORDER BY created_at DESC LIMIT 100''',
                      (lat - radius, lat + radius, lon - radius, lon + radius)).fetchall()
    return jsonify([dict(r) for r in logs])


@reports_bp.route('/api/quicklogs/<int:log_id>/vote', methods=['POST'])
def vote_quicklog(log_id):
    data = request.json
    if not data:
        return jsonify({'error': 'No data'}), 400
    vote_type = data.get('vote_type', 'up')
    if vote_type not in ('up', 'down'):
        return jsonify({'error': 'Invalid vote type'}), 400
    db = get_db()
    try:
        if vote_type == 'up':
            db.execute('UPDATE quick_logs SET upvotes=upvotes+1 WHERE id=?', (log_id,))
        else:
            db.execute('UPDATE quick_logs SET downvotes=downvotes+1 WHERE id=?', (log_id,))
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

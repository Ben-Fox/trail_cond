from flask import Flask, render_template, request, jsonify
import requests
import hashlib
import json
from database import get_db, init_db

app = Flask(__name__)
RIDB_KEY = 'b4cf5317-0be1-4127-97de-5bed2d3b0b68'
RIDB_BASE = 'https://ridb.recreation.gov/api/v1'

def ridb_get(endpoint, params=None):
    headers = {'apikey': RIDB_KEY, 'Accept': 'application/json'}
    if params is None:
        params = {}
    try:
        r = requests.get(f'{RIDB_BASE}{endpoint}', headers=headers, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"RIDB error: {e}")
        return None

# ─── Pages ───
@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/explore')
def explore():
    return render_template('explore.html')

@app.route('/facility/<facility_id>')
def facility_detail(facility_id):
    return render_template('facility.html', facility_id=facility_id)

@app.route('/report')
def report_page():
    return render_template('report.html')

# ─── API: Search ───
@app.route('/api/search')
def api_search():
    query = request.args.get('query', '')
    state = request.args.get('state', '')
    activity = request.args.get('activity', '')
    lat = request.args.get('lat', '')
    lon = request.args.get('lon', '')
    radius = request.args.get('radius', '50')
    limit = request.args.get('limit', '20')

    params = {'limit': limit, 'offset': 0}
    if query:
        params['query'] = query
    if state:
        params['state'] = state
    if activity:
        params['activity'] = activity
    if lat and lon:
        params['latitude'] = lat
        params['longitude'] = lon
        params['radius'] = radius

    data = ridb_get('/facilities', params)
    if not data:
        return jsonify({'results': []})

    facilities = data.get('RECDATA', [])
    results = []
    for f in facilities:
        results.append({
            'id': f.get('FacilityID', ''),
            'name': f.get('FacilityName', ''),
            'type': f.get('FacilityTypeDescription', ''),
            'description': f.get('FacilityDescription', ''),
            'lat': f.get('FacilityLatitude', 0),
            'lon': f.get('FacilityLongitude', 0),
            'state': f.get('AddressStateCode', ''),
            'reservable': f.get('Reservable', False),
            'enabled': f.get('Enabled', False),
            'phone': f.get('FacilityPhone', ''),
            'email': f.get('FacilityEmail', ''),
        })
    return jsonify({'results': results})

# ─── API: Facility Detail ───
@app.route('/api/facility/<facility_id>')
def api_facility(facility_id):
    data = ridb_get(f'/facilities/{facility_id}')
    if not data:
        return jsonify({'error': 'Not found'}), 404

    # Get addresses
    addrs = ridb_get(f'/facilities/{facility_id}/addresses')
    addresses = addrs.get('RECDATA', []) if addrs else []

    # Get activities
    acts = ridb_get(f'/facilities/{facility_id}/activities')
    activities = [a.get('ActivityName', '') for a in (acts.get('RECDATA', []) if acts else [])]

    # Get media/photos
    media = ridb_get(f'/facilities/{facility_id}/media')
    photos = [{'url': m.get('URL', ''), 'title': m.get('Title', '')} for m in (media.get('RECDATA', []) if media else [])]

    # Get condition summary from DB
    db = get_db()
    reports = db.execute('SELECT * FROM reports WHERE facility_id=? ORDER BY created_at DESC LIMIT 10', (str(facility_id),)).fetchall()
    report_list = [dict(r) for r in reports]
    db.close()

    facility = {
        'id': data.get('FacilityID', ''),
        'name': data.get('FacilityName', ''),
        'type': data.get('FacilityTypeDescription', ''),
        'description': data.get('FacilityDescription', ''),
        'directions': data.get('FacilityDirections', ''),
        'lat': data.get('FacilityLatitude', 0),
        'lon': data.get('FacilityLongitude', 0),
        'phone': data.get('FacilityPhone', ''),
        'email': data.get('FacilityEmail', ''),
        'reservable': data.get('Reservable', False),
        'enabled': data.get('Enabled', False),
        'ada': data.get('FacilityAdaAccess', ''),
        'addresses': [dict(a) for a in addresses],
        'activities': activities,
        'photos': photos,
        'reports': report_list,
    }
    return jsonify(facility)

# ─── API: Reports ───
@app.route('/api/facility/<facility_id>/reports', methods=['GET'])
def api_get_reports(facility_id):
    db = get_db()
    reports = db.execute('SELECT * FROM reports WHERE facility_id=? ORDER BY created_at DESC', (str(facility_id),)).fetchall()
    db.close()
    return jsonify([dict(r) for r in reports])

@app.route('/api/facility/<facility_id>/reports', methods=['POST'])
def api_add_report(facility_id):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    db = get_db()
    db.execute('''INSERT INTO reports (facility_id, facility_name, status, trail_condition, road_access, general_notes, date_visited)
                  VALUES (?, ?, ?, ?, ?, ?, ?)''',
               (str(facility_id), data.get('facility_name', ''), data.get('status', 'Good'),
                data.get('trail_condition', ''), data.get('road_access', ''),
                data.get('general_notes', ''), data.get('date_visited', '')))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/reports/<int:report_id>/vote', methods=['POST'])
def api_vote(report_id):
    data = request.get_json()
    vote_type = data.get('vote_type', 'up')
    ip_hash = hashlib.sha256(request.remote_addr.encode()).hexdigest()[:16]
    db = get_db()
    try:
        db.execute('INSERT INTO votes (report_id, ip_hash, vote_type) VALUES (?, ?, ?)',
                   (report_id, ip_hash, vote_type))
        col = 'upvotes' if vote_type == 'up' else 'downvotes'
        db.execute(f'UPDATE reports SET {col} = {col} + 1 WHERE id = ?', (report_id,))
        db.commit()
        db.close()
        return jsonify({'success': True})
    except Exception:
        db.close()
        return jsonify({'error': 'Already voted'}), 409

# ─── API: Weather ───
@app.route('/api/weather/<lat>/<lon>')
def api_weather(lat, lon):
    try:
        r = requests.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': lat, 'longitude': lon,
            'current_weather': 'true',
            'temperature_unit': 'fahrenheit',
            'windspeed_unit': 'mph'
        }, timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/weather/history/<lat>/<lon>')
def api_weather_history(lat, lon):
    from datetime import datetime, timedelta
    end = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    try:
        r = requests.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': lat, 'longitude': lon,
            'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,weathercode',
            'temperature_unit': 'fahrenheit',
            'start_date': start, 'end_date': end,
            'timezone': 'auto'
        }, timeout=10)
        data = r.json()
        # Condition inference
        daily = data.get('daily', {})
        inference = infer_conditions(daily)
        data['condition_inference'] = inference
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def infer_conditions(daily):
    precip = daily.get('precipitation_sum', [])
    snow = daily.get('snowfall_sum', [])
    temps = daily.get('temperature_2m_min', [])
    max_temps = daily.get('temperature_2m_max', [])

    total_precip = sum(p for p in precip if p)
    total_snow = sum(s for s in snow if s)
    min_temp = min(temps) if temps else 50
    avg_max = sum(max_temps) / len(max_temps) if max_temps else 60

    conditions = []
    color = 'green'
    if total_snow > 2:
        conditions.append('Likely snowy/icy conditions')
        color = 'red'
    elif total_snow > 0:
        conditions.append('Some snow reported')
        color = 'yellow'
    if total_precip > 1 and total_snow < 1:
        conditions.append('Recent rain — trails may be muddy')
        color = 'yellow' if color == 'green' else color
    if min_temp < 32:
        conditions.append('Below freezing temps — ice possible')
        if color == 'green':
            color = 'yellow'
    if total_precip < 0.1 and total_snow < 0.1 and min_temp > 32:
        conditions.append('Dry conditions — trails likely clear')
    if not conditions:
        conditions.append('Conditions appear moderate')

    return {
        'color': color,
        'conditions': conditions,
        'reasoning': f'{total_precip:.1f}" rain, {total_snow:.1f}" snow in 7 days. Low temp: {min_temp:.0f}F, Avg high: {avg_max:.0f}F'
    }

@app.route('/api/weather/batch')
def api_weather_batch():
    locations = request.args.get('locations', '')
    if not locations:
        return jsonify({})
    results = {}
    for loc in locations.split(';'):
        parts = loc.split(',')
        if len(parts) == 3:
            fid, lat, lon = parts
            try:
                r = requests.get('https://api.open-meteo.com/v1/forecast', params={
                    'latitude': lat, 'longitude': lon,
                    'current_weather': 'true',
                    'daily': 'precipitation_sum,snowfall_sum,temperature_2m_min,temperature_2m_max',
                    'temperature_unit': 'fahrenheit',
                    'windspeed_unit': 'mph',
                    'forecast_days': 1,
                    'past_days': 7,
                    'timezone': 'auto'
                }, timeout=10)
                wd = r.json()
                cw = wd.get('current_weather', {})
                daily = wd.get('daily', {})
                inf = infer_conditions(daily)
                results[fid] = {
                    'temp': cw.get('temperature', ''),
                    'weathercode': cw.get('weathercode', 0),
                    'windspeed': cw.get('windspeed', 0),
                    'inference': inf
                }
            except:
                pass
    return jsonify(results)

# ─── API: Trails ───
@app.route('/api/trails/nearby')
def api_trails_nearby():
    lat = request.args.get('lat', '')
    lon = request.args.get('lon', '')
    if not lat or not lon:
        return jsonify([])
    try:
        bbox = f'{float(lon)-0.05},{float(lat)-0.05},{float(lon)+0.05},{float(lat)+0.05}'
        r = requests.get(f'https://hiking.waymarkedtrails.org/api/v1/list/bbox', params={
            'bbox': bbox, 'limit': 10
        }, timeout=10)
        data = r.json()
        trails = []
        for t in data.get('results', data if isinstance(data, list) else []):
            tid = t.get('id', '')
            name = t.get('name', 'Unnamed Trail')
            # Get geometry
            try:
                gr = requests.get(f'https://hiking.waymarkedtrails.org/api/v1/details/route/{tid}/geometry/geojson', timeout=10)
                geojson = gr.json()
            except:
                geojson = None
            trails.append({'id': tid, 'name': name, 'geojson': geojson})
        return jsonify(trails)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── API: Activities ───
@app.route('/api/activities')
def api_activities():
    data = ridb_get('/activities', {'limit': 100})
    if not data:
        return jsonify([])
    return jsonify([{'id': a.get('ActivityID'), 'name': a.get('ActivityName')} for a in data.get('RECDATA', [])])

# ─── API: Standalone Reports ───
@app.route('/api/reports/standalone', methods=['POST'])
def api_standalone_report():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    db = get_db()
    db.execute('''INSERT INTO standalone_reports
        (facility_id, facility_name, state, area_region, date_visited, overall_condition,
         cleanliness, trail_surface, road_access, crowding, water_availability,
         restroom_condition, general_notes, hazards, recommend)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (data.get('facility_id', ''), data.get('facility_name', ''), data.get('state', ''),
         data.get('area_region', ''), data.get('date_visited', ''), data.get('overall_condition', ''),
         data.get('cleanliness', 0), data.get('trail_surface', ''), data.get('road_access', ''),
         data.get('crowding', ''), data.get('water_availability', ''),
         data.get('restroom_condition', ''), data.get('general_notes', ''),
         data.get('hazards', ''), data.get('recommend', '')))
    db.commit()
    db.close()
    return jsonify({'success': True})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8095, debug=True)

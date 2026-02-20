from flask import Flask, render_template, request, jsonify
import requests
import hashlib
from database import get_db, init_db
from datetime import datetime, timedelta

app = Flask(__name__)
RIDB_KEY = 'b4cf5317-0be1-4127-97de-5bed2d3b0b68'
RIDB_BASE = 'https://ridb.recreation.gov/api/v1'
TRAIL_TYPES = ['TRAIL', 'TRAILHEAD']

# --- Pages ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/explore')
def explore():
    return render_template('explore.html')

@app.route('/trail/<facility_id>')
def trail_detail(facility_id):
    return render_template('trail.html', facility_id=facility_id)

# --- API ---
@app.route('/api/search')
def api_search():
    q = request.args.get('q', '')
    state = request.args.get('state', '')
    lat = request.args.get('lat', '')
    lon = request.args.get('lon', '')
    limit = request.args.get('limit', '20')
    
    params = {'apikey': RIDB_KEY, 'limit': limit, 'offset': 0}
    if q:
        params['query'] = q
    if state:
        params['state'] = state
    if lat and lon:
        params['latitude'] = lat
        params['longitude'] = lon
        params['radius'] = 50
    
    try:
        r = requests.get(f'{RIDB_BASE}/facilities', params=params, timeout=10)
        data = r.json()
        facilities = data.get('RECDATA', [])
        trails = [f for f in facilities if f.get('FacilityTypeDescription', '').upper() in TRAIL_TYPES
                  or 'trail' in f.get('FacilityName', '').lower()
                  or 'HIKING' in str(f.get('ACTIVITY', '')).upper()]
        results = []
        for f in trails:
            results.append({
                'id': f.get('FacilityID'),
                'name': f.get('FacilityName', ''),
                'desc': f.get('FacilityDescription', '')[:200],
                'lat': f.get('FacilityLatitude'),
                'lon': f.get('FacilityLongitude'),
                'state': f.get('FACILITYADDRESS', [{}])[0].get('AddressStateCode', '') if f.get('FACILITYADDRESS') else '',
                'city': f.get('FACILITYADDRESS', [{}])[0].get('City', '') if f.get('FACILITYADDRESS') else '',
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trail/<facility_id>')
def api_trail(facility_id):
    try:
        params = {'apikey': RIDB_KEY}
        r = requests.get(f'{RIDB_BASE}/facilities/{facility_id}', params=params, timeout=10)
        f = r.json()
        
        # Get activities
        ar = requests.get(f'{RIDB_BASE}/facilities/{facility_id}/activities', params=params, timeout=10)
        activities = [a.get('ActivityName', '') for a in ar.json().get('RECDATA', [])]
        
        # Get media
        mr = requests.get(f'{RIDB_BASE}/facilities/{facility_id}/media', params=params, timeout=10)
        media = [{'url': m.get('URL', ''), 'title': m.get('Title', '')} for m in mr.json().get('RECDATA', [])]
        
        # Get addresses
        addr = requests.get(f'{RIDB_BASE}/facilities/{facility_id}/facilityaddresses', params=params, timeout=10)
        addresses = addr.json().get('RECDATA', [])
        
        result = {
            'id': f.get('FacilityID'),
            'name': f.get('FacilityName', ''),
            'desc': f.get('FacilityDescription', ''),
            'directions': f.get('FacilityDirections', ''),
            'lat': f.get('FacilityLatitude'),
            'lon': f.get('FacilityLongitude'),
            'phone': f.get('FacilityPhone', ''),
            'email': f.get('FacilityEmail', ''),
            'activities': activities,
            'media': media,
            'state': addresses[0].get('AddressStateCode', '') if addresses else '',
            'city': addresses[0].get('City', '') if addresses else '',
        }
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/weather/batch')
def api_weather_batch():
    locations = request.args.get('locations', '')  # "lat,lon|lat,lon|..."
    if not locations:
        return jsonify([])
    pairs = [l.split(',') for l in locations.split('|') if ',' in l]
    results = []
    for lat, lon in pairs[:20]:
        try:
            r = requests.get(f'https://api.open-meteo.com/v1/forecast', params={
                'latitude': lat, 'longitude': lon,
                'current_weather': 'true'
            }, timeout=8)
            w = r.json().get('current_weather', {})
            results.append({'lat': float(lat), 'lon': float(lon), 'temp_c': w.get('temperature'), 'windspeed': w.get('windspeed'), 'weathercode': w.get('weathercode')})
        except:
            results.append({'lat': float(lat), 'lon': float(lon), 'error': True})
    return jsonify(results)

@app.route('/api/weather/history')
def api_weather_history():
    lat = request.args.get('lat')
    lon = request.args.get('lon')
    if not lat or not lon:
        return jsonify({'error': 'lat/lon required'}), 400
    
    end = datetime.utcnow().date()
    start = end - timedelta(days=7)
    
    try:
        r = requests.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': lat, 'longitude': lon,
            'current_weather': 'true',
            'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,rain_sum,weathercode',
            'past_days': 7,
            'forecast_days': 1,
            'timezone': 'auto'
        }, timeout=10)
        data = r.json()
        current = data.get('current_weather', {})
        daily = data.get('daily', {})
        
        total_precip = sum(v for v in (daily.get('precipitation_sum') or []) if v) 
        total_snow = sum(v for v in (daily.get('snowfall_sum') or []) if v)
        total_rain = sum(v for v in (daily.get('rain_sum') or []) if v)
        min_temps = [v for v in (daily.get('temperature_2m_min') or []) if v is not None]
        avg_min = sum(min_temps) / len(min_temps) if min_temps else 0
        
        # Infer conditions
        reasons = []
        condition = 'clear'
        color = '#2d6a4f'
        
        if total_snow > 5:
            condition = 'snowy'
            color = '#4a90d9'
            reasons.append(f'{total_snow:.1f}cm of snow in last 7 days')
        elif total_snow > 0.5:
            if avg_min < 0:
                condition = 'icy'
                color = '#c44536'
                reasons.append(f'{total_snow:.1f}cm snow + freezing temps (avg low {avg_min:.0f}°C)')
            else:
                condition = 'snowy'
                color = '#4a90d9'
                reasons.append(f'{total_snow:.1f}cm of snow in last 7 days')
        
        if total_rain > 20:
            if condition == 'clear':
                condition = 'muddy'
                color = '#7f5539'
            reasons.append(f'{total_rain:.1f}mm rain in last 7 days — expect mud')
        elif total_rain > 5:
            if condition == 'clear':
                condition = 'wet'
                color = '#b08968'
            reasons.append(f'{total_rain:.1f}mm rain in last 7 days')
        
        if condition == 'clear' and total_precip < 2:
            condition = 'dry'
            color = '#2d6a4f'
            reasons.append('Minimal precipitation — trails likely dry')
        
        if not reasons:
            reasons.append('Conditions look good')
        
        # Badge level
        badge = 'green'
        if condition in ('wet', 'muddy'):
            badge = 'yellow'
        elif condition in ('snowy', 'icy'):
            badge = 'red'
        
        return jsonify({
            'current': {
                'temp_c': current.get('temperature'),
                'windspeed': current.get('windspeed'),
                'weathercode': current.get('weathercode'),
            },
            'daily': {
                'dates': daily.get('time', []),
                'temp_max': daily.get('temperature_2m_max', []),
                'temp_min': daily.get('temperature_2m_min', []),
                'precip': daily.get('precipitation_sum', []),
                'snow': daily.get('snowfall_sum', []),
                'rain': daily.get('rain_sum', []),
                'codes': daily.get('weathercode', []),
            },
            'inference': {
                'condition': condition,
                'badge': badge,
                'color': color,
                'reasons': reasons,
                'total_precip_mm': round(total_precip, 1),
                'total_snow_cm': round(total_snow, 1),
                'total_rain_mm': round(total_rain, 1),
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trails/nearby')
def api_trails_nearby():
    lat = request.args.get('lat')
    lon = request.args.get('lon')
    if not lat or not lon:
        return jsonify({'error': 'lat/lon required'}), 400
    try:
        bbox = f'{float(lon)-0.1},{float(lat)-0.1},{float(lon)+0.1},{float(lat)+0.1}'
        r = requests.get(f'https://hiking.waymarkedtrails.org/api/v1/list/search', params={
            'bbox': bbox, 'limit': 10
        }, timeout=10)
        return jsonify(r.json() if r.status_code == 200 else [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trail/<facility_id>/reports')
def get_reports(facility_id):
    db = get_db()
    reports = db.execute('SELECT * FROM reports WHERE facility_id=? ORDER BY created_at DESC', (facility_id,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in reports])

@app.route('/api/trail/<facility_id>/reports', methods=['POST'])
def add_report(facility_id):
    data = request.json
    db = get_db()
    db.execute('INSERT INTO reports (facility_id, trail_condition, trail_surface, road_access, general_notes, date_visited) VALUES (?,?,?,?,?,?)',
               (facility_id, data.get('trail_condition'), data.get('trail_surface'), data.get('road_access'), data.get('general_notes'), data.get('date_visited')))
    db.commit()
    db.close()
    return jsonify({'ok': True})

@app.route('/api/reports/<int:report_id>/vote', methods=['POST'])
def vote_report(report_id):
    data = request.json
    vote_type = data.get('vote_type', 'up')
    ip_hash = hashlib.sha256(request.remote_addr.encode()).hexdigest()[:16]
    db = get_db()
    try:
        existing = db.execute('SELECT vote_type FROM votes WHERE report_id=? AND ip_hash=?', (report_id, ip_hash)).fetchone()
        if existing:
            if existing['vote_type'] == vote_type:
                db.close()
                return jsonify({'error': 'Already voted'}), 400
            # Change vote
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
        db.close()
        return jsonify({'error': str(e)}), 500
    db.close()
    return jsonify({'ok': True})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8095, debug=False)

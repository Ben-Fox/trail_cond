from flask import Flask, render_template, request, jsonify
import requests
import hashlib
from database import init_db, add_report, get_reports, vote_report, get_condition_summary

app = Flask(__name__)

RIDB_BASE = 'https://ridb.recreation.gov/api/v1'
RIDB_KEY = 'b4cf5317-0be1-4127-97de-5bed2d3b0b68'
RIDB_HEADERS = {'apikey': RIDB_KEY}

def ridb_get(endpoint, params=None):
    try:
        r = requests.get(f'{RIDB_BASE}{endpoint}', headers=RIDB_HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {'error': str(e)}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/facility/<facility_id>')
def facility_page(facility_id):
    return render_template('facility.html', facility_id=facility_id)

@app.route('/api/search')
def api_search():
    params = {'limit': request.args.get('limit', 20)}
    if request.args.get('q'):
        params['query'] = request.args['q']
    if request.args.get('state'):
        params['state'] = request.args['state']
    if request.args.get('lat') and request.args.get('lon'):
        params['latitude'] = request.args['lat']
        params['longitude'] = request.args['lon']
        params['radius'] = request.args.get('radius', 50)
    if request.args.get('activity'):
        params['activity'] = request.args['activity']
    
    data = ridb_get('/facilities', params)
    if 'error' in data:
        return jsonify(data), 500
    
    facilities = data.get('RECDATA', [])
    results = []
    for f in facilities:
        results.append({
            'id': f.get('FacilityID'),
            'name': f.get('FacilityName'),
            'description': (f.get('FacilityDescription', '') or '')[:200],
            'lat': f.get('FacilityLatitude'),
            'lon': f.get('FacilityLongitude'),
            'type': f.get('FacilityTypeDescription'),
            'ada': f.get('FacilityAdaAccess'),
            'phone': f.get('FacilityPhone'),
            'email': f.get('FacilityEmail'),
            'media': [m.get('URL') for m in f.get('MEDIA', [])[:1]],
        })
    
    return jsonify({
        'results': results,
        'total': data.get('METADATA', {}).get('RESULTS', {}).get('TOTAL_COUNT', 0)
    })

@app.route('/api/facility/<facility_id>')
def api_facility(facility_id):
    data = ridb_get(f'/facilities/{facility_id}')
    if 'error' in data:
        return jsonify(data), 500
    
    # Also get activities
    activities = ridb_get(f'/facilities/{facility_id}/activities')
    activity_list = [a.get('ActivityName') for a in activities.get('RECDATA', [])] if 'RECDATA' in activities else []
    
    # Get media
    media = ridb_get(f'/facilities/{facility_id}/media')
    media_list = [{'url': m.get('URL'), 'title': m.get('Title')} for m in media.get('RECDATA', [])] if 'RECDATA' in media else []
    
    # Get condition summary
    summary = get_condition_summary(facility_id)
    
    result = {
        'id': data.get('FacilityID'),
        'name': data.get('FacilityName'),
        'description': data.get('FacilityDescription', ''),
        'directions': data.get('FacilityDirections', ''),
        'lat': data.get('FacilityLatitude'),
        'lon': data.get('FacilityLongitude'),
        'type': data.get('FacilityTypeDescription'),
        'ada': data.get('FacilityAdaAccess'),
        'ada_text': data.get('FacilityAccessibilityText', ''),
        'phone': data.get('FacilityPhone'),
        'email': data.get('FacilityEmail'),
        'reservation_url': data.get('FacilityReservationURL'),
        'activities': activity_list,
        'media': media_list,
        'condition_summary': summary,
    }
    return jsonify(result)

@app.route('/api/facility/<facility_id>/reports')
def api_get_reports(facility_id):
    reports = get_reports(facility_id)
    return jsonify({'reports': reports})

@app.route('/api/facility/<facility_id>/reports', methods=['POST'])
def api_add_report(facility_id):
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    report_id = add_report(facility_id, data)
    return jsonify({'id': report_id, 'success': True}), 201

@app.route('/api/reports/<int:report_id>/vote', methods=['POST'])
def api_vote(report_id):
    data = request.json
    vote_type = data.get('vote', 'up')
    ip_hash = hashlib.md5(request.remote_addr.encode()).hexdigest()[:8]
    result = vote_report(report_id, vote_type, ip_hash)
    if result is False:
        return jsonify({'error': 'Already voted'}), 409
    return jsonify(result)

@app.route('/api/weather')
def api_weather():
    lat = request.args.get('lat')
    lon = request.args.get('lon')
    if not lat or not lon:
        return jsonify({'error': 'lat and lon required'}), 400
    try:
        r = requests.get(
            f'https://api.open-meteo.com/v1/forecast',
            params={
                'latitude': lat, 'longitude': lon,
                'current_weather': 'true',
                'temperature_unit': 'fahrenheit',
                'windspeed_unit': 'mph',
            },
            timeout=5
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/weather/history')
def api_weather_history():
    lat = request.args.get('lat')
    lon = request.args.get('lon')
    if not lat or not lon:
        return jsonify({'error': 'lat and lon required'}), 400
    try:
        from datetime import datetime, timedelta
        end = datetime.utcnow().date()
        start = end - timedelta(days=7)
        r = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={
                'latitude': lat, 'longitude': lon,
                'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,rain_sum',
                'temperature_unit': 'fahrenheit',
                'precipitation_unit': 'inch',
                'timezone': 'auto',
                'start_date': start.isoformat(),
                'end_date': end.isoformat(),
            },
            timeout=5
        )
        data = r.json()
        daily = data.get('daily', {})

        # Compute inference
        temps_min = [t for t in (daily.get('temperature_2m_min') or []) if t is not None]
        temps_max = [t for t in (daily.get('temperature_2m_max') or []) if t is not None]
        snow_vals = [s for s in (daily.get('snowfall_sum') or []) if s is not None]
        rain_vals = [r for r in (daily.get('rain_sum') or []) if r is not None]

        total_snow = sum(snow_vals)
        total_rain = sum(rain_vals)
        avg_min = sum(temps_min) / len(temps_min) if temps_min else 40
        avg_max = sum(temps_max) / len(temps_max) if temps_max else 60
        any_freezing = any(t <= 32 for t in temps_min) if temps_min else False

        reasons = []
        level = 'green'  # green / yellow / red

        if total_snow > 2:
            reasons.append(f'{total_snow:.1f}" of snow in the last 7 days')
            level = 'red'
        elif total_snow > 0.5:
            reasons.append(f'{total_snow:.1f}" of snow in the last 7 days')
            level = 'yellow' if level != 'red' else level

        if total_rain > 1.5:
            reasons.append(f'{total_rain:.1f}" of rain in the last 7 days')
            if level == 'green':
                level = 'yellow'
        elif total_rain > 0.5:
            reasons.append(f'{total_rain:.1f}" of rain recently')

        if any_freezing:
            reasons.append(f'Below-freezing lows (avg low {avg_min:.0f}\u00b0F)')
            if level == 'green':
                level = 'yellow'

        if total_snow > 2:
            prediction = 'Trails likely snowy/icy'
        elif total_snow > 0.5 and any_freezing:
            prediction = 'Watch for ice on trails'
        elif total_rain > 1.5:
            prediction = 'Trails may be muddy/wet'
        elif total_rain > 0.5:
            prediction = 'Some wet spots possible'
        elif any_freezing and total_rain > 0:
            prediction = 'Watch for ice'
        elif avg_max > 50 and total_rain < 0.3 and total_snow < 0.1:
            prediction = 'Trails likely dry and clear'
            reasons.append(f'Dry conditions, avg high {avg_max:.0f}\u00b0F')
        else:
            prediction = 'Conditions appear moderate'
            if not reasons:
                reasons.append(f'Avg high {avg_max:.0f}\u00b0F, avg low {avg_min:.0f}\u00b0F')

        return jsonify({
            'daily': daily,
            'inference': {
                'prediction': prediction,
                'level': level,
                'reasons': reasons,
                'total_snow_inches': round(total_snow, 1),
                'total_rain_inches': round(total_rain, 1),
                'avg_high': round(avg_max),
                'avg_low': round(avg_min),
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/activities')
def api_activities():
    data = ridb_get('/activities', {'limit': 100})
    if 'error' in data:
        return jsonify(data), 500
    activities = [{'id': a.get('ActivityID'), 'name': a.get('ActivityName')} for a in data.get('RECDATA', [])]
    return jsonify({'activities': activities})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8095, debug=False)

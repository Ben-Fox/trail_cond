from flask import Blueprint, request, jsonify

from services import http_session

elevation_bp = Blueprint('elevation', __name__)


@elevation_bp.route('/api/elevation')
def api_elevation():
    lats = request.args.get('lats', '')
    lons = request.args.get('lons', '')
    if not lats or not lons:
        return jsonify({'error': 'Missing lats/lons'}), 400

    try:
        lat_list = lats.split(',')
        lon_list = lons.split(',')
        if len(lat_list) != len(lon_list):
            return jsonify({'error': 'Mismatched lats/lons count'}), 400
        # Validate all are numeric
        for v in lat_list + lon_list:
            float(v.strip())

        elevations = []
        for i in range(0, len(lat_list), 100):
            batch_lats = ','.join(lat_list[i:i+100])
            batch_lons = ','.join(lon_list[i:i+100])
            r = http_session.get('https://api.open-meteo.com/v1/elevation', params={
                'latitude': batch_lats,
                'longitude': batch_lons,
            }, timeout=10)
            data = r.json()
            elevations.extend(data.get('elevation', []))

        return jsonify({'elevation': elevations})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

"""
USGS Stream/River Gauge Data — finds nearby stream gauges for trails
and returns current water levels (gage height + streamflow).
"""

import time
import logging
from flask import Blueprint, request, jsonify
from services import http_session

logger = logging.getLogger(__name__)

streams_bp = Blueprint('streams', __name__)

USGS_URL = 'https://waterservices.usgs.gov/nwis/iv/'

# Cache: (lat,lon) rounded to 0.1° -> (timestamp, data)
_stream_cache = {}
CACHE_TTL = 900  # 15 minutes


def _gauge_level_label(gage_height):
    """Classify water level from gage height."""
    if gage_height is None:
        return 'Unknown', '#888'
    h = float(gage_height)
    if h < 0 or h == -999999:
        return 'No Data', '#888'
    if h < 1.0:
        return 'Very Low', '#2d6a4f'
    if h < 3.0:
        return 'Low', '#52b788'
    if h < 5.0:
        return 'Moderate', '#b08968'
    if h < 8.0:
        return 'High', '#e76f51'
    return 'Very High', '#c44536'


def _flow_label(cfs):
    """Classify streamflow from cubic feet per second."""
    if cfs is None:
        return 'Unknown'
    f = float(cfs)
    if f < 0:
        return 'No Data'
    if f < 10:
        return 'Trickle'
    if f < 100:
        return 'Light Flow'
    if f < 500:
        return 'Moderate Flow'
    if f < 2000:
        return 'Strong Flow'
    return 'Dangerous Flow'


def fetch_nearby_gauges(lat, lon, radius_deg=0.15):
    """Fetch USGS stream gauges near a point. Returns list of gauge dicts."""
    cache_key = (round(lat, 1), round(lon, 1))
    now = time.time()
    if cache_key in _stream_cache:
        ts, data = _stream_cache[cache_key]
        if now - ts < CACHE_TTL:
            return data

    try:
        west = lon - radius_deg
        south = lat - radius_deg
        east = lon + radius_deg
        north = lat + radius_deg

        resp = http_session.get(USGS_URL, params={
            'format': 'json',
            'bBox': f'{west:.4f},{south:.4f},{east:.4f},{north:.4f}',
            'parameterCd': '00065,00060',  # gage height + streamflow
            'siteStatus': 'active',
        }, timeout=10)

        data = resp.json()
        time_series = data.get('value', {}).get('timeSeries', [])

        # Group by site
        sites = {}
        for ts_item in time_series:
            info = ts_item['sourceInfo']
            site_code = info['siteCode'][0]['value']
            var_code = ts_item['variable']['variableCode'][0]['value']
            loc = info['geoLocation']['geogLocation']
            values = ts_item['values'][0]['value']
            latest = values[-1] if values else {}
            val = latest.get('value')

            if site_code not in sites:
                sites[site_code] = {
                    'site_code': site_code,
                    'name': info['siteName'],
                    'lat': loc['latitude'],
                    'lon': loc['longitude'],
                    'gage_height': None,
                    'gage_height_label': 'Unknown',
                    'gage_height_color': '#888',
                    'streamflow': None,
                    'streamflow_label': 'Unknown',
                    'datetime': latest.get('dateTime', ''),
                }

            if var_code == '00065' and val and float(val) != -999999:
                h = float(val)
                label, color = _gauge_level_label(h)
                sites[site_code]['gage_height'] = round(h, 2)
                sites[site_code]['gage_height_label'] = label
                sites[site_code]['gage_height_color'] = color
            elif var_code == '00060' and val and float(val) != -999999:
                f = float(val)
                sites[site_code]['streamflow'] = round(f, 1)
                sites[site_code]['streamflow_label'] = _flow_label(f)

        result = sorted(sites.values(), key=lambda s: (
            (s['lat'] - lat) ** 2 + (s['lon'] - lon) ** 2
        ))[:8]  # max 8 nearest

        _stream_cache[cache_key] = (now, result)
        return result

    except Exception as e:
        logger.error(f"USGS stream gauge error: {e}")
        return []


@streams_bp.route('/api/streams')
def api_streams():
    """Get nearby stream gauges for a lat/lon."""
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        if lat is None or lon is None:
            return jsonify({'error': 'lat and lon required'}), 400

        gauges = fetch_nearby_gauges(lat, lon)
        return jsonify({'gauges': gauges, 'count': len(gauges)})
    except Exception as e:
        logger.error(f"Stream gauge API error: {e}")
        return jsonify({'gauges': [], 'count': 0, 'error': 'Service temporarily unavailable'}), 200

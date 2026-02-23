"""
NPS Trail Alerts — fetches alerts from National Park Service API
and matches them to nearby trails by park location.
"""

import time
import math
import logging
from flask import Blueprint, request, jsonify
from services import http_session

logger = logging.getLogger(__name__)

npsalerts_bp = Blueprint('npsalerts', __name__)

NPS_API_URL = 'https://developer.nps.gov/api/v1'
NPS_API_KEY = 'DEMO_KEY'  # Free tier, 1000 req/hour

# Cache: state_code -> (timestamp, alerts)
_alert_cache = {}
CACHE_TTL = 1800  # 30 minutes

# Park location cache: park_code -> {lat, lon, name}
_park_cache = {}
PARK_CACHE_TTL = 86400  # 24 hours
_park_cache_ts = 0


ALERT_ICONS = {
    'Danger': {'icon': 'danger', 'color': '#c44536'},
    'Caution': {'icon': 'caution', 'color': '#e76f51'},
    'Park Closure': {'icon': 'closure', 'color': '#c44536'},
    'Information': {'icon': 'info', 'color': '#4a90d9'},
}


def _haversine_km(lat1, lon1, lat2, lon2):
    """Distance in km between two points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _state_from_coords(lat, lon):
    """Rough state code lookup from lat/lon (US only)."""
    # Simplified bounding boxes for western hiking states
    state_boxes = [
        ('CO', 37.0, 41.0, -109.05, -102.05),
        ('UT', 37.0, 42.0, -114.05, -109.05),
        ('CA', 32.5, 42.0, -124.4, -114.1),
        ('WA', 45.5, 49.0, -124.8, -116.9),
        ('OR', 42.0, 46.3, -124.6, -116.5),
        ('MT', 44.4, 49.0, -116.1, -104.0),
        ('WY', 41.0, 45.0, -111.1, -104.1),
        ('ID', 42.0, 49.0, -117.2, -111.0),
        ('AZ', 31.3, 37.0, -114.8, -109.0),
        ('NM', 31.3, 37.0, -109.05, -103.0),
        ('NV', 35.0, 42.0, -120.0, -114.0),
        ('TX', 25.8, 36.5, -106.6, -93.5),
        ('NC', 33.8, 36.6, -84.3, -75.5),
        ('TN', 35.0, 36.7, -90.3, -81.6),
        ('VA', 36.5, 39.5, -83.7, -75.2),
        ('WV', 37.2, 40.6, -82.6, -77.7),
        ('ME', 43.1, 47.5, -71.1, -66.9),
        ('NH', 42.7, 45.3, -72.6, -71.0),
        ('VT', 42.7, 45.0, -73.4, -71.5),
        ('NY', 40.5, 45.0, -79.8, -71.9),
        ('PA', 39.7, 42.3, -80.5, -74.7),
    ]
    for code, s, n, w, e in state_boxes:
        if s <= lat <= n and w <= lon <= e:
            return code
    return 'CO'  # fallback


def _load_park_locations(state_code):
    """Load park locations for a state from NPS API."""
    global _park_cache_ts
    now = time.time()

    # Check if we need to refresh
    if now - _park_cache_ts < PARK_CACHE_TTL and _park_cache:
        return

    try:
        resp = http_session.get(f'{NPS_API_URL}/parks', params={
            'stateCode': state_code,
            'limit': 100,
            'fields': 'addresses',
            'api_key': NPS_API_KEY,
        }, timeout=10)
        data = resp.json()
        for park in data.get('data', []):
            lat = park.get('latitude', '')
            lon = park.get('longitude', '')
            if lat and lon:
                try:
                    _park_cache[park['parkCode']] = {
                        'lat': float(lat),
                        'lon': float(lon),
                        'name': park['fullName'],
                        'code': park['parkCode'],
                    }
                except (ValueError, KeyError):
                    pass
        _park_cache_ts = now
    except Exception as e:
        logger.error(f"NPS park location error: {e}")


def fetch_nearby_alerts(lat, lon, radius_km=80):
    """Fetch NPS alerts near a point. Returns alerts from parks within radius."""
    state_code = _state_from_coords(lat, lon)
    now = time.time()

    # Load park locations
    _load_park_locations(state_code)

    # Check alert cache
    if state_code in _alert_cache:
        ts, cached_alerts = _alert_cache[state_code]
        if now - ts < CACHE_TTL:
            return _filter_alerts_by_distance(cached_alerts, lat, lon, radius_km)

    try:
        resp = http_session.get(f'{NPS_API_URL}/alerts', params={
            'stateCode': state_code,
            'limit': 50,
            'api_key': NPS_API_KEY,
        }, timeout=10)
        data = resp.json()
        alerts = []

        for a in data.get('data', []):
            park_code = a.get('parkCode', '')
            park_info = _park_cache.get(park_code, {})
            style = ALERT_ICONS.get(a.get('category', ''), ALERT_ICONS['Information'])

            alert = {
                'id': a.get('id', ''),
                'title': a.get('title', ''),
                'description': a.get('description', ''),
                'category': a.get('category', 'Information'),
                'url': a.get('url', ''),
                'park_code': park_code,
                'park_name': park_info.get('name', a.get('parkCode', '').upper()),
                'park_lat': park_info.get('lat'),
                'park_lon': park_info.get('lon'),
                'color': style['color'],
                'last_updated': a.get('lastIndexedDate', ''),
            }
            alerts.append(alert)

        _alert_cache[state_code] = (now, alerts)
        return _filter_alerts_by_distance(alerts, lat, lon, radius_km)

    except Exception as e:
        logger.error(f"NPS alerts error: {e}")
        return []


def _filter_alerts_by_distance(alerts, lat, lon, radius_km):
    """Filter alerts to only those from parks within radius of the point."""
    nearby = []
    for a in alerts:
        if a.get('park_lat') and a.get('park_lon'):
            dist = _haversine_km(lat, lon, a['park_lat'], a['park_lon'])
            if dist <= radius_km:
                a_copy = dict(a)
                a_copy['distance_km'] = round(dist, 1)
                nearby.append(a_copy)
        else:
            # No park location — include with unknown distance
            a_copy = dict(a)
            a_copy['distance_km'] = None
            nearby.append(a_copy)

    return sorted(nearby, key=lambda x: x.get('distance_km') or 999)


@npsalerts_bp.route('/api/alerts')
def api_alerts():
    """Get NPS alerts near a lat/lon."""
    try:
        lat = request.args.get('lat', type=float)
        lon = request.args.get('lon', type=float)
        radius = request.args.get('radius', 80, type=float)

        if lat is None or lon is None:
            return jsonify({'error': 'lat and lon required'}), 400

        alerts = fetch_nearby_alerts(lat, lon, radius)
        return jsonify({'alerts': alerts, 'count': len(alerts)})
    except Exception as e:
        logger.error(f"NPS alerts API error: {e}")
        return jsonify({'alerts': [], 'count': 0, 'error': 'Service temporarily unavailable'}), 200

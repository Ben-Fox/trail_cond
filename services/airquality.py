"""
Air Quality tile overlay — Open-Meteo Air Quality API.

Renders AQI as a smooth colored overlay, same tile-based approach as condtiles.py.
US AQI scale:
  0-50 Good (green), 51-100 Moderate (yellow), 101-150 Unhealthy Sensitive (orange),
  151-200 Unhealthy (red), 201-300 Very Unhealthy (purple), 301+ Hazardous (maroon)

Toggle on/off independently from conditions overlay.
"""
import io
import time
import math
import logging
from flask import Blueprint, Response, request, jsonify

from services import http_session
from services.cache import cached_response, cache_response

logger = logging.getLogger(__name__)

airquality_bp = Blueprint('airquality', __name__)

# Tile cache: (z, x, y) → (timestamp, png_bytes)
_aq_tile_cache = {}
AQ_TILE_CACHE_TTL = 1800  # 30 min

# AQI point cache: "lat,lon" → (timestamp, aqi_data)
_aq_point_cache = {}
AQ_POINT_CACHE_TTL = 900  # 15 min

# Official EPA AQI color ramp (exact RGB values from EPA style guide)
# Same colors used by AirNow, Weather.com, IQAir, etc.
# Interpolation method: IDW (Inverse Distance Weighting) — same as EPA AirNow contour maps
# Source: EPA AirNow Mapping Fact Sheet + archive.epa.gov/ttn/ozone/web/pdf/rg701.pdf
AQI_COLORS = [
    (0,   ( 45, 154,  45, 80)),   # Good - darkened green #2D9A2D
    (50,  ( 45, 154,  45, 90)),
    (51,  (212, 160,  23, 100)),  # Moderate - amber #D4A017
    (100, (212, 160,  23, 110)),
    (101, (255, 126,   0, 120)),  # USG - EPA orange #FF7E00
    (150, (255, 126,   0, 130)),
    (151, (255,   0,   0, 130)),  # Unhealthy - EPA red #FF0000
    (200, (255,   0,   0, 140)),
    (201, (143,  63, 151, 140)),  # Very Unhealthy - EPA purple #8F3F97
    (300, (143,  63, 151, 150)),
    (301, (126,   0,  35, 160)),  # Hazardous - EPA maroon #7E0023
    (500, (126,   0,  35, 170)),
]

AQI_LABELS = {
    0: 'Good',
    51: 'Moderate',
    101: 'Unhealthy for Sensitive Groups',
    151: 'Unhealthy',
    201: 'Very Unhealthy',
    301: 'Hazardous',
}


def _aqi_label(aqi):
    """Get AQI category label."""
    if aqi <= 50: return 'Good'
    if aqi <= 100: return 'Moderate'
    if aqi <= 150: return 'Unhealthy for Sensitive Groups'
    if aqi <= 200: return 'Unhealthy'
    if aqi <= 300: return 'Very Unhealthy'
    return 'Hazardous'


def _aqi_color_rgba(aqi):
    """Interpolate RGBA from AQI value."""
    aqi = max(0, min(500, aqi))
    for i in range(len(AQI_COLORS) - 1):
        lo_val, lo_c = AQI_COLORS[i]
        hi_val, hi_c = AQI_COLORS[i + 1]
        if lo_val <= aqi <= hi_val:
            t = (aqi - lo_val) / max(1, hi_val - lo_val)
            return tuple(int(lo_c[j] + (hi_c[j] - lo_c[j]) * t) for j in range(4))
    return AQI_COLORS[-1][1]


def _tile_to_bbox(z, x, y):
    n = 2 ** z
    lon_w = x / n * 360 - 180
    lon_e = (x + 1) / n * 360 - 180
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lat_s, lat_n, lon_w, lon_e


def _fetch_aqi_grid(lat_s, lat_n, lon_w, lon_e, samples=4):
    """Fetch current US AQI for a grid of points. Returns list of (lat, lon, aqi)."""
    points = []
    for r in range(samples):
        for c in range(samples):
            lat = lat_s + (lat_n - lat_s) * (r + 0.5) / samples
            lon = lon_w + (lon_e - lon_w) * (c + 0.5) / samples
            points.append((round(lat, 3), round(lon, 3)))

    # Check point cache first
    now = time.time()
    results = []
    uncached = []
    for i, (lat, lon) in enumerate(points):
        key = f"{lat:.2f},{lon:.2f}"
        if key in _aq_point_cache and now - _aq_point_cache[key][0] < AQ_POINT_CACHE_TTL:
            results.append((lat, lon, _aq_point_cache[key][1]))
        else:
            results.append(None)
            uncached.append(i)

    if uncached:
        batch_lats = ','.join(str(points[i][0]) for i in uncached)
        batch_lons = ','.join(str(points[i][1]) for i in uncached)
        try:
            r = http_session.get('https://air-quality-api.open-meteo.com/v1/air-quality', params={
                'latitude': batch_lats,
                'longitude': batch_lons,
                'current': 'us_aqi',
            }, timeout=12)
            data = r.json()
            if not isinstance(data, list):
                data = [data]

            for j, d in enumerate(data):
                idx = uncached[j]
                aqi = d.get('current', {}).get('us_aqi', 0) or 0
                lat, lon = points[idx]
                results[idx] = (lat, lon, aqi)
                key = f"{lat:.2f},{lon:.2f}"
                _aq_point_cache[key] = (now, aqi)
        except Exception as e:
            logger.warning(f'AQ tile fetch failed: {e}')
            for j in uncached:
                if results[j] is None:
                    results[j] = (points[j][0], points[j][1], 0)

    return results


def _render_aq_tile(scored_points, lat_s, lat_n, lon_w, lon_e, size=256):
    """Render AQI overlay tile using IDW interpolation."""
    from PIL import Image, ImageFilter

    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    pixels = img.load()

    lat_range = lat_n - lat_s
    lon_range = lon_e - lon_w
    power = 2.5

    pt_lats = [p[0] for p in scored_points]
    pt_lons = [p[1] for p in scored_points]
    pt_aqi = [p[2] for p in scored_points]
    n_pts = len(scored_points)

    for py in range(size):
        lat = lat_n - (py / size) * lat_range
        for px in range(size):
            lon = lon_w + (px / size) * lon_range

            total_w = 0
            w_aqi = 0

            for i in range(n_pts):
                dlat = (lat - pt_lats[i]) * 111
                dlon = (lon - pt_lons[i]) * 85
                dsq = dlat * dlat + dlon * dlon
                if dsq < 0.01:
                    w_aqi = pt_aqi[i]
                    total_w = 1
                    break
                wt = 1.0 / (dsq ** (power / 2))
                total_w += wt
                w_aqi += wt * pt_aqi[i]

            aqi = w_aqi / total_w if total_w > 0 else 0
            pixels[px, py] = _aqi_color_rgba(aqi)

    img = img.filter(ImageFilter.GaussianBlur(radius=8))
    return img


@airquality_bp.route('/api/tiles/airquality/<int:z>/<int:x>/<int:y>.png')
def airquality_tile(z, x, y):
    """Serve an air quality overlay tile."""
    from PIL import Image

    if z < 4 or z > 14:
        img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, 'PNG', optimize=True)
        return Response(buf.getvalue(), mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=3600'})

    cache_key = (z, x, y)
    now = time.time()
    if cache_key in _aq_tile_cache:
        ts, png_bytes = _aq_tile_cache[cache_key]
        if now - ts < AQ_TILE_CACHE_TTL:
            return Response(png_bytes, mimetype='image/png',
                            headers={'Cache-Control': f'public, max-age={AQ_TILE_CACHE_TTL}'})

    # Prune old cache
    if len(_aq_tile_cache) > 2000:
        stale = [k for k, (ts, _) in _aq_tile_cache.items() if now - ts > AQ_TILE_CACHE_TTL]
        for k in stale:
            del _aq_tile_cache[k]

    lat_s, lat_n, lon_w, lon_e = _tile_to_bbox(z, x, y)
    samples = 3 if z <= 7 else 4 if z <= 10 else 5

    scored = _fetch_aqi_grid(lat_s, lat_n, lon_w, lon_e, samples=samples)
    img = _render_aq_tile(scored, lat_s, lat_n, lon_w, lon_e)

    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    png_bytes = buf.getvalue()

    _aq_tile_cache[cache_key] = (now, png_bytes)

    return Response(png_bytes, mimetype='image/png',
                    headers={'Cache-Control': f'public, max-age={AQ_TILE_CACHE_TTL}'})


@airquality_bp.route('/api/airquality')
def api_airquality():
    """Get detailed air quality for a specific point."""
    lat = request.args.get('lat')
    lon = request.args.get('lon')
    if not lat or not lon:
        return jsonify({'error': 'lat/lon required'}), 400

    cache_key = f"aq:{float(lat):.2f},{float(lon):.2f}"
    cached = cached_response(cache_key, ttl=900)
    if cached:
        return jsonify(cached)

    try:
        r = http_session.get('https://air-quality-api.open-meteo.com/v1/air-quality', params={
            'latitude': lat,
            'longitude': lon,
            'current': 'us_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone',
        }, timeout=10)
        data = r.json()
        current = data.get('current', {})

        aqi = current.get('us_aqi', 0) or 0
        result = {
            'us_aqi': aqi,
            'label': _aqi_label(aqi),
            'pm2_5': current.get('pm2_5'),
            'pm10': current.get('pm10'),
            'ozone': current.get('ozone'),
            'no2': current.get('nitrogen_dioxide'),
            'so2': current.get('sulphur_dioxide'),
            'co': current.get('carbon_monoxide'),
        }
        cache_response(cache_key, result, ttl=900)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

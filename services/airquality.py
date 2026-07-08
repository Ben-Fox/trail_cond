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
import threading

import numpy as np
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

_aq_lock = threading.Lock()

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


# The upstream air-quality data (Open-Meteo / CAMS) is only ~0.25 deg resolution,
# so sampling finer than that adds NO real information, only interpolation artifacts
# and an extra zoom band transition. We cap the finest lattice at the data's own
# resolution. Above z7 the node set is therefore FROZEN (same 0.25 deg grid at every
# zoom), so the field cannot shift as you zoom in past that point.
_AQ_DATA_SPACING = 0.25

# Interpolation neighborhood in degrees. Every tile fetches all nodes within this
# distance of its edges, so the set of nodes feeding any given point is the same at
# every zoom (at high zoom a tiny tile would otherwise see only 2-3 nodes and drift).
_AQ_MARGIN_DEG = 1.5


def _aq_lattice_spacing(z):
    """Two grid-aligned, NESTED levels only: 0.5 deg when far out (z<=6) for fetch
    cost, then 0.25 deg (the data's true resolution) for z>=7. Grid alignment plus a
    2x ratio means the coarse grid's nodes are a strict SUBSET of the fine grid's, so
    the single z6->z7 step only ADDS nodes (refines) rather than repainting, and from
    z7 up the node set never changes at all."""
    return 0.5 if z <= 6 else _AQ_DATA_SPACING


def _aq_lattice_points(lat_s, lat_n, lon_w, lon_e, spacing):
    """Grid-aligned lattice (nodes at i*spacing, not offset by half a cell) plus a
    fixed-DEGREE neighborhood ring (_AQ_MARGIN_DEG). Grid alignment + power-of-two
    spacings make the levels nest; the fixed-degree ring (rather than a fixed cell
    count) keeps the surrounding node set constant in geographic terms across zooms,
    so a point's interpolated value does not drift when a zoomed-in tile would
    otherwise capture too few neighbors."""
    margin = max(2, math.ceil(_AQ_MARGIN_DEG / spacing))
    i0 = math.floor(lat_s / spacing) - margin
    i1 = math.floor(lat_n / spacing) + margin
    j0 = math.floor(lon_w / spacing) - margin
    j1 = math.floor(lon_e / spacing) + margin
    pts = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            lat = i * spacing
            lon = j * spacing
            if -85 < lat < 85:
                pts.append((i, j, lat, lon))
    return pts


def _aq_ckey(lat, lon):
    """Cache key = physical coordinates (rounded), NOT lattice index. Two zoom
    bands can share a node (nested grids), and keying on coordinates makes them
    reuse one cached value, so a shared node reads identically at any zoom. It
    also removes the old bug where the same (i,j) index meant different real
    locations at different spacings and served wrong-location AQI after a zoom."""
    return (round(lat, 4), round(lon, 4))


def _get_aq_points(cells):
    """Current US AQI per lattice cell; batch-fetches only missing cells."""
    now = time.time()
    out = {}
    missing = []
    with _aq_lock:
        for (i, j, lat, lon) in cells:
            e = _aq_point_cache.get(_aq_ckey(lat, lon))
            if e is not None and now - e[0] < AQ_POINT_CACHE_TTL:
                out[(i, j)] = e[1]
            else:
                missing.append((i, j, lat, lon))

    for k0 in range(0, len(missing), 100):
        chunk = missing[k0:k0 + 100]
        data = None
        for attempt in range(2):  # one retry so a transient hiccup doesn't leave a gap
            try:
                r = http_session.get('https://air-quality-api.open-meteo.com/v1/air-quality', params={
                    'latitude': ','.join(f'{p[2]:.4f}' for p in chunk),
                    'longitude': ','.join(f'{p[3]:.4f}' for p in chunk),
                    'current': 'us_aqi',
                }, timeout=12)
                data = r.json()
                if not isinstance(data, list):
                    data = [data]
                break
            except Exception as e:
                if attempt == 1:
                    logger.warning(f'AQ point fetch failed ({len(chunk)} pts): {e}')
        if data is None:
            continue
        with _aq_lock:
            for p, d in zip(chunk, data):
                aqi = float(d.get('current', {}).get('us_aqi', 0) or 0)
                _aq_point_cache[_aq_ckey(p[2], p[3])] = (now, aqi)
                out[(p[0], p[1])] = aqi

    with _aq_lock:
        if len(_aq_point_cache) > 20000:
            stale = sorted(_aq_point_cache.items(), key=lambda kv: kv[1][0])
            for k, _ in stale[:len(_aq_point_cache) - 20000]:
                _aq_point_cache.pop(k, None)
    return out


# vectorized EPA color ramp lookup tables
_AQI_X = np.array([v for v, _ in AQI_COLORS], dtype=np.float32)
_AQI_R = np.array([c[0] for _, c in AQI_COLORS], dtype=np.float32)
_AQI_G = np.array([c[1] for _, c in AQI_COLORS], dtype=np.float32)
_AQI_B = np.array([c[2] for _, c in AQI_COLORS], dtype=np.float32)
_AQI_A = np.array([c[3] for _, c in AQI_COLORS], dtype=np.float32)


def _render_aq_tile_v2(cells, values, lat_s, lat_n, lon_w, lon_e, size=256, k=12):
    """numpy k-nearest IDW over the AQI field + EPA ramp.

    Each pixel is interpolated from ONLY its k nearest data nodes, not every node in
    the tile. Because the nodes sit on a fixed world grid, a given point's k nearest
    neighbors are the same set at any zoom, so its color does not drift as you zoom in
    (all-neighbor IDW let the shrinking tile's far nodes nudge the value each step)."""
    from PIL import Image, ImageFilter

    pts = [(lat, lon, values[(i, j)]) for (i, j, lat, lon) in cells if (i, j) in values]
    if not pts:
        return Image.new('RGBA', (size, size), (0, 0, 0, 0))

    p_lat = np.array([p[0] for p in pts], dtype=np.float32)
    p_lon = np.array([p[1] for p in pts], dtype=np.float32)
    p_aqi = np.array([p[2] for p in pts], dtype=np.float32)

    lat_v = np.linspace(lat_n, lat_s, size, dtype=np.float32)[:, None, None]
    lon_v = np.linspace(lon_w, lon_e, size, dtype=np.float32)[None, :, None]
    km_lon = 111.0 * math.cos(math.radians((lat_s + lat_n) / 2))
    dlat = (lat_v - p_lat[None, None, :]) * 111.0
    dlon = (lon_v - p_lon[None, None, :]) * km_lon
    d2 = dlat * dlat + dlon * dlon + 1e-6  # (H, W, N)

    kk = min(k, d2.shape[2])
    idx = np.argpartition(d2, kk - 1, axis=2)[..., :kk]  # k nearest node indices per pixel
    d2k = np.take_along_axis(d2, idx, axis=2)
    aqik = p_aqi[idx]
    w = 1.0 / (d2k ** 1.25)
    aqi = (w * aqik).sum(axis=2) / w.sum(axis=2)

    out = np.empty((size, size, 4), dtype=np.uint8)
    out[..., 0] = np.interp(aqi, _AQI_X, _AQI_R).astype(np.uint8)
    out[..., 1] = np.interp(aqi, _AQI_X, _AQI_G).astype(np.uint8)
    out[..., 2] = np.interp(aqi, _AQI_X, _AQI_B).astype(np.uint8)
    out[..., 3] = np.interp(aqi, _AQI_X, _AQI_A).astype(np.uint8)

    img = Image.fromarray(out, 'RGBA')
    return img.filter(ImageFilter.GaussianBlur(radius=1.5))


_aq_inflight = {}


@airquality_bp.route('/api/tiles/airquality/<int:z>/<int:x>/<int:y>.png')
def airquality_tile(z, x, y):
    """AQ overlay tile from a nested, grid-aligned lattice (seamless + zoom-stable)."""
    from PIL import Image

    def _blank(max_age=3600):
        img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, 'PNG', optimize=True)
        return Response(buf.getvalue(), mimetype='image/png',
                        headers={'Cache-Control': f'public, max-age={max_age}'})

    if z < 4 or z > 14:
        return _blank()

    key = (z, x, y)
    now = time.time()
    while True:
        with _aq_lock:
            entry = _aq_tile_cache.get(key)
            if entry and now - entry[0] < AQ_TILE_CACHE_TTL:
                return Response(entry[1], mimetype='image/png',
                                headers={'Cache-Control': f'public, max-age={AQ_TILE_CACHE_TTL}'})
            ev = _aq_inflight.get(key)
            if ev is None:
                _aq_inflight[key] = threading.Event()
                break
        ev.wait(timeout=20)
        now = time.time()

    try:
        lat_s, lat_n, lon_w, lon_e = _tile_to_bbox(z, x, y)
        cells = _aq_lattice_points(lat_s, lat_n, lon_w, lon_e, _aq_lattice_spacing(z))
        values = _get_aq_points(cells)
        if not values:
            return _blank(300)
        img = _render_aq_tile_v2(cells, values, lat_s, lat_n, lon_w, lon_e)
        buf = io.BytesIO()
        img.save(buf, 'PNG', optimize=True)
        png = buf.getvalue()
        with _aq_lock:
            _aq_tile_cache[key] = (time.time(), png)
            if len(_aq_tile_cache) > 2000:
                stale = sorted(_aq_tile_cache.items(), key=lambda kv: kv[1][0])
                for k, _ in stale[:len(_aq_tile_cache) - 2000]:
                    _aq_tile_cache.pop(k, None)
        return Response(png, mimetype='image/png',
                        headers={'Cache-Control': f'public, max-age={AQ_TILE_CACHE_TTL}'})
    except Exception as e:
        logger.warning(f'AQ tile render failed for {z}/{x}/{y}: {e}')
        return _blank(300)
    finally:
        with _aq_lock:
            ev = _aq_inflight.pop(key, None)
        if ev:
            ev.set()


@airquality_bp.route('/api/airquality')
def api_airquality():
    """Get detailed air quality for a specific point."""
    lat = request.args.get('lat')
    lon = request.args.get('lon')
    if not lat or not lon:
        return jsonify({'error': 'lat/lon required'}), 400

    try:
        cache_key = f"aq:{float(lat):.2f},{float(lon):.2f}"
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid lat/lon'}), 400
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
    except Exception:
        return jsonify({'error': 'Air quality data temporarily unavailable'}), 500

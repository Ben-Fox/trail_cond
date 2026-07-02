"""
Condition tile server — weather condition overlay tiles (rebuilt 2026-07-02).

Methodology (v2):
  - A GLOBAL sample lattice (zoom-banded spacing, snapped to fixed grid indices)
    replaces per-tile sample points. Neighboring tiles share lattice points and
    each tile includes a margin ring, so interpolation is SEAMLESS across tiles.
  - Weather per lattice point is cached once (30 min) and shared by every tile
    that touches it: a full map view costs one or two upstream calls instead of
    one per tile, and panning only fetches genuinely new points.
  - Interpolation runs on CONTINUOUS physical fields (moisture mm, snow index,
    freeze fraction) with numpy-vectorized IDW, then classifies per pixel.
    Interpolating classes directly (old approach) invented conditions: halfway
    between snowy and dry is NOT muddy. Fields in, classes out.
  - Color is a smooth ramp within each regime (dry->wet->muddy by moisture,
    snow->ice by freeze) so the overlay reads like a real weather map.

Endpoint contract unchanged: /api/tiles/conditions/{z}/{x}/{y}.png
"""
import io
import time
import math
import logging
import threading

import numpy as np
from flask import Blueprint, Response

from services import http_session

logger = logging.getLogger(__name__)

condtiles_bp = Blueprint('condtiles', __name__)

TILE_CACHE_TTL = 1800          # rendered PNGs
POINT_CACHE_TTL = 1800         # per-lattice-point weather fields
MAX_TILE_CACHE = 2000
MAX_POINT_CACHE = 20000

_tile_cache = {}               # (z,x,y) -> (ts, png)
_point_cache = {}              # (spacing_key, i, j) -> (ts, fields dict)
_inflight = {}                 # (z,x,y) -> threading.Event
_lock = threading.Lock()

# ---------------------------------------------------------------- lattice

def _lattice_spacing(z):
    """Sample spacing in degrees. Only TWO band transitions (z6->7, z9->10) so
    the rendered field is geographically stable while zooming within a band;
    finest capped at 0.03125 deg (~3 km) = forecast model resolution."""
    if z <= 6:
        return 0.5
    if z <= 9:
        return 0.125
    return 0.03125


def _tile_to_bbox(z, x, y):
    n = 2 ** z
    lon_w = x / n * 360 - 180
    lon_e = (x + 1) / n * 360 - 180
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lat_s, lat_n, lon_w, lon_e


def _lattice_points(lat_s, lat_n, lon_w, lon_e, spacing, margin=2):
    """Snapped lattice cell indices covering bbox plus a margin ring."""
    i0 = math.floor(lat_s / spacing) - margin
    i1 = math.floor(lat_n / spacing) + margin
    j0 = math.floor(lon_w / spacing) - margin
    j1 = math.floor(lon_e / spacing) + margin
    pts = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            lat = (i + 0.5) * spacing
            lon = (j + 0.5) * spacing
            if -85 < lat < 85:
                pts.append((i, j, lat, lon))
    return pts

# ---------------------------------------------------------------- physics

def _compute_fields(daily, current=None, elevation=0):
    """Moisture-budget simulation -> continuous fields.
    Returns dict(moisture=mm, snow=index ~inches, freeze=0..1)."""
    rain_days = daily.get('rain_sum') or []
    snow_days = daily.get('snowfall_sum') or []
    temp_max = daily.get('temperature_2m_max') or []
    temp_min = daily.get('temperature_2m_min') or []
    et0_days = daily.get('et0_fao_evapotranspiration') or []
    solar_days = daily.get('shortwave_radiation_sum') or []
    wind_days = daily.get('windspeed_10m_max') or []

    n_days = len(rain_days)
    if n_days == 0:
        return dict(moisture=0.0, snow=0.0, freeze=0.0)

    snow_depth_m = (current or {}).get('snow_depth', 0) or 0
    snow_depth_in = snow_depth_m * 39.37
    current_temp = (current or {}).get('temperature_2m')
    elev_ft = (elevation or 0) * 3.281

    moisture = 0.0
    snow_on_ground = 0.0   # cm

    for i in range(n_days):
        rain = (rain_days[i] or 0)
        snow = (snow_days[i] or 0)
        t_max = (temp_max[i] if i < len(temp_max) and temp_max[i] is not None else 10)
        t_min = (temp_min[i] if i < len(temp_min) and temp_min[i] is not None else 0)
        et0 = (et0_days[i] if i < len(et0_days) and et0_days[i] is not None else 1.5)
        solar = (solar_days[i] if i < len(solar_days) and solar_days[i] is not None else 10)
        wind = (wind_days[i] if i < len(wind_days) and wind_days[i] is not None else 10)

        avg_temp = (t_max + t_min) / 2
        moisture += rain
        snow_on_ground += snow

        if avg_temp > 2 and snow_on_ground > 0:
            melt = min(snow_on_ground, (avg_temp - 2) * 0.5)
            snow_on_ground -= melt
            moisture += melt * 0.8

        drying = et0
        if avg_temp > 10:
            drying *= 1.0 + (avg_temp - 10) * 0.08
        else:
            drying *= max(0.3, 0.5 + avg_temp * 0.05)
        drying *= max(0.5, solar / 15.0)
        drying *= 1.0 + max(0, wind - 10) * 0.02
        if elev_ft > 8000:
            drying *= 0.6
        elif elev_ft > 6000:
            drying *= 0.8
        moisture = max(0, moisture - drying)

    # freeze fraction: current temp below zero, or recent minima below zero
    mins = [v for v in temp_min if v is not None]
    avg_min = sum(mins) / len(mins) if mins else 5.0
    freeze = 0.0
    if current_temp is not None and current_temp < 0:
        freeze = 1.0
    elif avg_min < -2:
        freeze = 1.0
    elif avg_min < 1:
        freeze = (1 - avg_min) / 3.0

    snow_idx = max(snow_depth_in, snow_on_ground * 0.5)
    return dict(moisture=float(moisture), snow=float(snow_idx), freeze=float(freeze))

# ---------------------------------------------------------------- point cache

def _get_point_fields(cells, spacing):
    """Fields for lattice cells, fetching only what's missing in ONE batch."""
    skey = round(spacing, 6)
    now = time.time()
    out = {}
    missing = []
    with _lock:
        for (i, j, lat, lon) in cells:
            e = _point_cache.get((skey, i, j))
            if e is not None and now - e[0] < POINT_CACHE_TTL:
                out[(i, j)] = e[1]
            else:
                missing.append((i, j, lat, lon))

    for chunk_start in range(0, len(missing), 100):
        chunk = missing[chunk_start:chunk_start + 100]
        lats = ','.join(f'{p[2]:.4f}' for p in chunk)
        lons = ','.join(f'{p[3]:.4f}' for p in chunk)
        try:
            r = http_session.get('https://api.open-meteo.com/v1/forecast', params={
                'latitude': lats, 'longitude': lons,
                'daily': 'precipitation_sum,snowfall_sum,rain_sum,temperature_2m_max,'
                         'temperature_2m_min,shortwave_radiation_sum,'
                         'et0_fao_evapotranspiration,windspeed_10m_max',
                'current': 'snow_depth,temperature_2m',
                'past_days': 7, 'forecast_days': 1, 'timezone': 'auto'
            }, timeout=12)
            data = r.json()
            if not isinstance(data, list):
                data = [data]
            with _lock:
                for p, d in zip(chunk, data):
                    fields = _compute_fields(d.get('daily', {}), d.get('current', {}),
                                             d.get('elevation', 0))
                    _point_cache[(skey, p[0], p[1])] = (now, fields)
                    out[(p[0], p[1])] = fields
        except Exception as e:
            logger.warning(f'Condition point fetch failed ({len(chunk)} pts): {e}')

    with _lock:
        if len(_point_cache) > MAX_POINT_CACHE:
            stale = sorted(_point_cache.items(), key=lambda kv: kv[1][0])
            for k, _ in stale[:len(_point_cache) - MAX_POINT_CACHE]:
                _point_cache.pop(k, None)
    return out

# ---------------------------------------------------------------- rendering

# color anchors
_C_DRY = np.array([82, 183, 136], dtype=np.float32)
_C_WET = np.array([176, 137, 104], dtype=np.float32)
_C_MUD = np.array([127, 85, 57], dtype=np.float32)
_C_SNOW = np.array([74, 144, 217], dtype=np.float32)
_C_ICE = np.array([196, 69, 54], dtype=np.float32)


def _render_tile_v2(cells, fields, lat_s, lat_n, lon_w, lon_e, size=256):
    """numpy IDW over continuous fields -> per-pixel classify -> smooth RGBA."""
    from PIL import Image, ImageFilter

    pts = [(lat, lon, fields[(i, j)]) for (i, j, lat, lon) in cells if (i, j) in fields]
    if not pts:
        return Image.new('RGBA', (size, size), (0, 0, 0, 0))

    p_lat = np.array([p[0] for p in pts], dtype=np.float32)
    p_lon = np.array([p[1] for p in pts], dtype=np.float32)
    f_moist = np.array([p[2]['moisture'] for p in pts], dtype=np.float32)
    f_snow = np.array([p[2]['snow'] for p in pts], dtype=np.float32)
    f_freeze = np.array([p[2]['freeze'] for p in pts], dtype=np.float32)

    lat_v = np.linspace(lat_n, lat_s, size, dtype=np.float32)[:, None, None]
    lon_v = np.linspace(lon_w, lon_e, size, dtype=np.float32)[None, :, None]
    km_per_deg_lon = 111.0 * math.cos(math.radians((lat_s + lat_n) / 2))
    dlat = (lat_v - p_lat[None, None, :]) * 111.0
    dlon = (lon_v - p_lon[None, None, :]) * km_per_deg_lon
    dsq = dlat * dlat + dlon * dlon + 1e-6
    w = 1.0 / (dsq ** 1.25)                       # IDW power 2.5 on distance
    wsum = w.sum(axis=2)
    moist = (w * f_moist).sum(axis=2) / wsum
    snow = (w * f_snow).sum(axis=2) / wsum
    freeze = (w * f_freeze).sum(axis=2) / wsum

    # moisture ramp: dry(<=2) -> wet(5) -> mud(12+)
    t_wet = np.clip((moist - 2.0) / 3.0, 0, 1)[..., None]
    t_mud = np.clip((moist - 5.0) / 7.0, 0, 1)[..., None]
    rgb = _C_DRY * (1 - t_wet) + _C_WET * t_wet
    rgb = rgb * (1 - t_mud) + _C_MUD * t_mud
    alpha = 60 + np.clip(moist / 12.0, 0, 1) * 60          # 60..120

    # snow regime blends over the moisture colors; ice where freezing
    snow_w = np.clip((snow - 0.5) / 1.5, 0, 1)[..., None]
    c_frozen = _C_SNOW * (1 - freeze[..., None]) + _C_ICE * freeze[..., None]
    rgb = rgb * (1 - snow_w) + c_frozen * snow_w
    alpha = alpha * (1 - snow_w[..., 0]) + (90 + 40 * np.clip(snow / 6, 0, 1)) * snow_w[..., 0]

    out = np.empty((size, size, 4), dtype=np.uint8)
    out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(alpha, 0, 140).astype(np.uint8)

    img = Image.fromarray(out, 'RGBA')
    return img.filter(ImageFilter.GaussianBlur(radius=1.5))


def _transparent_png():
    from PIL import Image
    img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    return buf.getvalue()

# ---------------------------------------------------------------- route

@condtiles_bp.route('/api/tiles/conditions/<int:z>/<int:x>/<int:y>.png')
def condition_tile(z, x, y):
    if z < 4 or z > 14:
        return Response(_transparent_png(), mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=3600'})

    key = (z, x, y)
    now = time.time()

    # cache / inflight-dedup: only one thread renders a given tile
    while True:
        with _lock:
            entry = _tile_cache.get(key)
            if entry and now - entry[0] < TILE_CACHE_TTL:
                return Response(entry[1], mimetype='image/png',
                                headers={'Cache-Control': f'public, max-age={TILE_CACHE_TTL}'})
            ev = _inflight.get(key)
            if ev is None:
                _inflight[key] = threading.Event()
                break
        ev.wait(timeout=20)
        now = time.time()

    try:
        lat_s, lat_n, lon_w, lon_e = _tile_to_bbox(z, x, y)
        spacing = _lattice_spacing(z)
        cells = _lattice_points(lat_s, lat_n, lon_w, lon_e, spacing)
        fields = _get_point_fields(cells, spacing)
        if fields:
            img = _render_tile_v2(cells, fields, lat_s, lat_n, lon_w, lon_e)
            buf = io.BytesIO()
            img.save(buf, 'PNG', optimize=True)
            png = buf.getvalue()
            ttl_ok = True
        else:
            png = _transparent_png()
            ttl_ok = False   # upstream failed: short-cache so it retries soon

        with _lock:
            _tile_cache[key] = (time.time() if ttl_ok else time.time() - TILE_CACHE_TTL + 120, png)
            if len(_tile_cache) > MAX_TILE_CACHE:
                stale = sorted(_tile_cache.items(), key=lambda kv: kv[1][0])
                for k, _ in stale[:len(_tile_cache) - MAX_TILE_CACHE]:
                    _tile_cache.pop(k, None)
        return Response(png, mimetype='image/png',
                        headers={'Cache-Control': f'public, max-age={TILE_CACHE_TTL if ttl_ok else 120}'})
    finally:
        with _lock:
            ev = _inflight.pop(key, None)
        if ev:
            ev.set()

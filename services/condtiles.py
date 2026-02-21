"""
Condition tile server — generates weather condition map tiles on demand.

Uses the same approach as real weather maps: pre-rendered tiles served as
a standard Leaflet TileLayer ({z}/{x}/{y}.png). Each tile is 256x256 pixels,
cached for 30 minutes. Weather data from Open-Meteo 7-day history.

The tile is a smooth color overlay showing inferred trail conditions:
  green=dry, tan=wet, brown=muddy, blue=snowy, red=icy
"""
import io
import time
import math
import logging
from flask import Blueprint, Response

from services import http_session

logger = logging.getLogger(__name__)

condtiles_bp = Blueprint('condtiles', __name__)

# Tile cache: (z, x, y) → (timestamp, png_bytes)
_tile_cache = {}
TILE_CACHE_TTL = 1800  # 30 minutes — weather doesn't change fast

# Condition inference
COND_SCORE = {'clear': 0, 'dry': 0, 'wet': 1, 'muddy': 2, 'snowy': 3, 'icy': 4}
COND_RGBA = {
    0: (82, 183, 136, 90),    # dry/clear - green
    1: (176, 137, 104, 100),  # wet - tan
    2: (127, 85, 57, 110),    # muddy - brown
    3: (74, 144, 217, 110),   # snowy - blue
    4: (196, 69, 54, 120),    # icy - red
}


def _tile_to_bbox(z, x, y):
    """Convert tile coords to lat/lon bounding box."""
    n = 2 ** z
    lon_w = x / n * 360 - 180
    lon_e = (x + 1) / n * 360 - 180
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lat_s, lat_n, lon_w, lon_e


def _lerp_color(score):
    """Interpolate RGBA color from condition score."""
    lo = max(0, min(3, int(score)))
    hi = min(4, lo + 1)
    t = score - lo
    a, b = COND_RGBA[lo], COND_RGBA[hi]
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(4))


def _infer_condition(daily):
    """Infer trail condition from 7-day weather data. Returns score (0-4)."""
    total_snow = sum(v for v in (daily.get('snowfall_sum') or []) if v)
    total_rain = sum(v for v in (daily.get('rain_sum') or []) if v)
    total_precip = sum(v for v in (daily.get('precipitation_sum') or []) if v)
    min_temps = [v for v in (daily.get('temperature_2m_min') or []) if v is not None]
    avg_min = sum(min_temps) / len(min_temps) if min_temps else 0

    if total_snow > 5:
        return COND_SCORE['snowy']
    if total_snow > 0.5:
        return COND_SCORE['icy'] if avg_min < 0 else COND_SCORE['snowy']
    if total_rain > 20:
        return COND_SCORE['muddy']
    if total_rain > 5:
        return COND_SCORE['wet']
    if total_precip < 2:
        return COND_SCORE['dry']
    return COND_SCORE['clear']


def _fetch_grid_conditions(lat_s, lat_n, lon_w, lon_e, samples=4):
    """Fetch weather for a grid of points within bbox. Returns list of (lat, lon, score)."""
    points = []
    for r in range(samples):
        for c in range(samples):
            lat = lat_s + (lat_n - lat_s) * (r + 0.5) / samples
            lon = lon_w + (lon_e - lon_w) * (c + 0.5) / samples
            points.append((round(lat, 3), round(lon, 3)))

    lats = ','.join(str(p[0]) for p in points)
    lons = ','.join(str(p[1]) for p in points)

    try:
        r = http_session.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': lats,
            'longitude': lons,
            'daily': 'precipitation_sum,snowfall_sum,rain_sum,temperature_2m_min',
            'past_days': 7,
            'forecast_days': 1,
            'timezone': 'auto'
        }, timeout=12)
        data = r.json()

        if not isinstance(data, list):
            data = [data]

        scored = []
        for i, d in enumerate(data):
            daily = d.get('daily', {})
            score = _infer_condition(daily)
            scored.append((points[i][0], points[i][1], score))
        return scored
    except Exception as e:
        logger.warning(f'Condition tile weather fetch failed: {e}')
        return [(p[0], p[1], 0) for p in points]


def _render_tile(scored_points, lat_s, lat_n, lon_w, lon_e, size=256):
    """Render a 256x256 RGBA tile using IDW from scored points."""
    from PIL import Image

    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    pixels = img.load()

    lat_range = lat_n - lat_s
    lon_range = lon_e - lon_w
    power = 2.5
    n_pts = len(scored_points)

    # Pre-extract for speed
    pt_lats = [p[0] for p in scored_points]
    pt_lons = [p[1] for p in scored_points]
    pt_scores = [p[2] for p in scored_points]

    for py in range(size):
        lat = lat_n - (py / size) * lat_range
        for px in range(size):
            lon = lon_w + (px / size) * lon_range

            total_w = 0
            w_score = 0

            for i in range(n_pts):
                dlat = (lat - pt_lats[i]) * 111
                dlon = (lon - pt_lons[i]) * 85
                dsq = dlat * dlat + dlon * dlon
                if dsq < 0.01:
                    w_score = pt_scores[i]
                    total_w = 1
                    break
                wt = 1.0 / (dsq ** (power / 2))
                total_w += wt
                w_score += wt * pt_scores[i]

            score = w_score / total_w if total_w > 0 else 0
            pixels[px, py] = _lerp_color(score)

    # Apply Gaussian blur for smooth look
    from PIL import ImageFilter
    img = img.filter(ImageFilter.GaussianBlur(radius=8))

    return img


@condtiles_bp.route('/api/tiles/conditions/<int:z>/<int:x>/<int:y>.png')
def condition_tile(z, x, y):
    """Serve a condition overlay tile."""
    # Only render for reasonable zoom levels
    if z < 4 or z > 14:
        # Return transparent tile
        from PIL import Image
        img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, 'PNG', optimize=True)
        return Response(buf.getvalue(), mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=3600'})

    # Check cache
    cache_key = (z, x, y)
    now = time.time()
    if cache_key in _tile_cache:
        ts, png_bytes = _tile_cache[cache_key]
        if now - ts < TILE_CACHE_TTL:
            return Response(png_bytes, mimetype='image/png',
                            headers={'Cache-Control': f'public, max-age={TILE_CACHE_TTL}'})

    # Prune old cache entries
    if len(_tile_cache) > 2000:
        stale = [k for k, (ts, _) in _tile_cache.items() if now - ts > TILE_CACHE_TTL]
        for k in stale:
            del _tile_cache[k]

    # Get tile bbox
    lat_s, lat_n, lon_w, lon_e = _tile_to_bbox(z, x, y)

    # Adaptive sample density based on zoom
    # Low zoom = fewer samples per tile (covers large area, coarse is fine)
    # High zoom = more samples (small area, want detail)
    samples = 3 if z <= 7 else 4 if z <= 10 else 5

    # Fetch conditions
    scored = _fetch_grid_conditions(lat_s, lat_n, lon_w, lon_e, samples=samples)

    # Render tile
    img = _render_tile(scored, lat_s, lat_n, lon_w, lon_e)

    # Save to PNG bytes
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    png_bytes = buf.getvalue()

    # Cache
    _tile_cache[cache_key] = (now, png_bytes)

    return Response(png_bytes, mimetype='image/png',
                    headers={'Cache-Control': f'public, max-age={TILE_CACHE_TTL}'})

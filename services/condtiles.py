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


def _infer_condition(daily, current=None, elevation=0):
    """
    Moisture Budget Model — simulates day-by-day trail moisture.
    
    Instead of just summing 7-day totals, we track how wet the ground is
    each day by adding precipitation and subtracting drying. This handles:
    - Rain 2 days ago + hot/sunny since = dry
    - Rain 2 days ago + cool/cloudy since = still muddy
    - Snow a week ago at high elevation = still snowy
    - Light rain + high evapotranspiration = already dry
    
    Uses Open-Meteo's et0_fao_evapotranspiration (scientific evaporation rate),
    solar radiation, temperature, and wind to compute drying.
    """
    rain_days = daily.get('rain_sum') or []
    snow_days = daily.get('snowfall_sum') or []
    temp_max = daily.get('temperature_2m_max') or []
    temp_min = daily.get('temperature_2m_min') or []
    et0_days = daily.get('et0_fao_evapotranspiration') or []
    solar_days = daily.get('shortwave_radiation_sum') or []
    wind_days = daily.get('windspeed_10m_max') or []
    
    n_days = len(rain_days)
    if n_days == 0:
        return COND_SCORE['clear']
    
    # Current snow depth (meters) from Open-Meteo
    snow_depth_m = 0
    if current:
        snow_depth_m = current.get('snow_depth', 0) or 0
    snow_depth_in = snow_depth_m * 39.37
    elev_ft = (elevation or 0) * 3.281
    
    # --- Snow check first (snow depth is the strongest signal) ---
    if snow_depth_in > 12:
        return COND_SCORE['snowy']  # Deep snow on ground, definitely snowy
    if snow_depth_in > 3:
        # Some snow — check if it's freezing (icy) or just snowy
        current_temp = (current or {}).get('temperature_2m', 0) or 0
        if current_temp < 0:
            return COND_SCORE['icy']
        return COND_SCORE['snowy']
    
    # --- Moisture budget simulation ---
    # Walk through each day, accumulating moisture and subtracting drying
    moisture = 0.0  # mm of "effective wetness" on the trail
    snow_on_ground = 0.0  # cm of accumulated snow
    
    for i in range(n_days):
        rain = (rain_days[i] or 0)
        snow = (snow_days[i] or 0)
        t_max = (temp_max[i] if i < len(temp_max) and temp_max[i] is not None else 10)
        t_min = (temp_min[i] if i < len(temp_min) and temp_min[i] is not None else 0)
        et0 = (et0_days[i] if i < len(et0_days) and et0_days[i] is not None else 1.5)
        solar = (solar_days[i] if i < len(solar_days) and solar_days[i] is not None else 10)
        wind = (wind_days[i] if i < len(wind_days) and wind_days[i] is not None else 10)
        
        avg_temp = (t_max + t_min) / 2
        
        # Add precipitation to moisture
        moisture += rain
        snow_on_ground += snow
        
        # Snowmelt adds to moisture if warm enough
        if avg_temp > 2 and snow_on_ground > 0:
            melt_rate = min(snow_on_ground, (avg_temp - 2) * 0.5)  # degree-day melt
            snow_on_ground -= melt_rate
            moisture += melt_rate * 0.8  # snow water equivalent
        
        # Drying calculation — multiple factors
        # Base: evapotranspiration is the scientific standard (mm/day of evaporation)
        drying = et0
        
        # Temperature boost: hot days dry trails much faster
        # Below 10°C: minimal extra drying. Above 25°C: rapid drying.
        if avg_temp > 10:
            temp_factor = 1.0 + (avg_temp - 10) * 0.08  # +8% per degree above 10°C
        else:
            temp_factor = max(0.3, 0.5 + avg_temp * 0.05)  # cold = slow drying
        drying *= temp_factor
        
        # Solar radiation boost: sunny days vs cloudy
        # Typical range: 5 (overcast winter) to 30 (clear summer)
        solar_factor = max(0.5, solar / 15.0)  # normalize around 15 MJ/m²
        drying *= solar_factor
        
        # Wind boost: wind accelerates evaporation
        wind_factor = 1.0 + max(0, wind - 10) * 0.02  # +2% per km/h above 10
        drying *= wind_factor
        
        # High elevation penalty: trails dry slower at altitude (thinner air, 
        # often shadowed, snow lingers)
        if elev_ft > 8000:
            drying *= 0.6
        elif elev_ft > 6000:
            drying *= 0.8
        
        # Apply drying (can't go below 0)
        moisture = max(0, moisture - drying)
    
    # --- Classify based on remaining moisture + snow ---
    # Also factor in recent heavy snowfall even if snow_depth sensor reads low
    total_snow = sum(v for v in snow_days if v)
    
    if snow_on_ground > 5 or total_snow > 8:
        return COND_SCORE['snowy']
    if snow_on_ground > 1:
        avg_min = sum(v for v in (temp_min or []) if v is not None) / max(1, len([v for v in (temp_min or []) if v is not None]))
        return COND_SCORE['icy'] if avg_min < 0 else COND_SCORE['snowy']
    
    # Moisture thresholds for mud/wet/dry
    if moisture > 12:
        return COND_SCORE['muddy']   # saturated ground
    if moisture > 5:
        return COND_SCORE['wet']     # noticeably damp
    if moisture > 2:
        return COND_SCORE['clear']   # slightly damp but fine
    return COND_SCORE['dry']         # bone dry


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
            'daily': 'precipitation_sum,snowfall_sum,rain_sum,temperature_2m_max,temperature_2m_min,shortwave_radiation_sum,et0_fao_evapotranspiration,windspeed_10m_max',
            'current': 'snow_depth,temperature_2m',
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
            current = d.get('current', {})
            elevation = d.get('elevation', 0)
            score = _infer_condition(daily, current=current, elevation=elevation)
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

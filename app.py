from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_compress import Compress
import requests
import hashlib
import time
import os
from math import radians, sin, cos, sqrt, atan2
from database import get_db, init_db

app = Flask(__name__)

# Gzip/Brotli compression for all responses
Compress(app)
app.config['COMPRESS_ALGORITHM'] = ['br', 'gzip']
app.config['COMPRESS_MIMETYPES'] = [
    'text/html', 'text/css', 'text/javascript', 'application/javascript',
    'application/json', 'image/svg+xml'
]

# Static asset cache headers (24 hours)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400

# Reuse HTTP connections to external APIs (connection pooling)
http_session = requests.Session()
http_session.headers.update({'User-Agent': 'TrailCondish/1.0'})
RIDB_KEY = 'b4cf5317-0be1-4127-97de-5bed2d3b0b68'
RIDB_BASE = 'https://ridb.recreation.gov/api/v1'
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'

STATE_NAMES = {
    'AL':'Alabama','AK':'Alaska','AZ':'Arizona','AR':'Arkansas','CA':'California',
    'CO':'Colorado','CT':'Connecticut','DE':'Delaware','FL':'Florida','GA':'Georgia',
    'HI':'Hawaii','ID':'Idaho','IL':'Illinois','IN':'Indiana','IA':'Iowa',
    'KS':'Kansas','KY':'Kentucky','LA':'Louisiana','ME':'Maine','MD':'Maryland',
    'MA':'Massachusetts','MI':'Michigan','MN':'Minnesota','MS':'Mississippi','MO':'Missouri',
    'MT':'Montana','NE':'Nebraska','NV':'Nevada','NH':'New Hampshire','NJ':'New Jersey',
    'NM':'New Mexico','NY':'New York','NC':'North Carolina','ND':'North Dakota','OH':'Ohio',
    'OK':'Oklahoma','OR':'Oregon','PA':'Pennsylvania','RI':'Rhode Island','SC':'South Carolina',
    'SD':'South Dakota','TN':'Tennessee','TX':'Texas','UT':'Utah','VT':'Vermont',
    'VA':'Virginia','WA':'Washington','WV':'West Virginia','WI':'Wisconsin','WY':'Wyoming'
}

# Simple in-memory cache for Overpass results
_overpass_cache = {}
CACHE_TTL = 600  # 10 minutes

# API response cache
_api_cache = {}
API_CACHE_TTL = 300  # 5 minutes

def cached_response(key, ttl=API_CACHE_TTL):
    """Check if a cached API response exists and is fresh."""
    now = time.time()
    if key in _api_cache:
        ts, data = _api_cache[key]
        if now - ts < ttl:
            return data
    return None

def cache_response(key, data, ttl=API_CACHE_TTL):
    """Store an API response in cache."""
    _api_cache[key] = (time.time(), data)
    # Prune if too large
    if len(_api_cache) > 500:
        cutoff = time.time() - ttl
        stale = [k for k, (ts, _) in _api_cache.items() if ts < cutoff]
        for k in stale:
            del _api_cache[k]
    return data

@app.after_request
def add_cache_headers(response):
    """Add cache headers for static assets and API responses."""
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400'
    elif request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'public, max-age=60'
    # Security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'

def geocode(query):
    """Use Nominatim to geocode a query, return dict with lat/lon/bbox or None.
    Fetches multiple results and prefers natural features / parks."""
    r = http_session.get(NOMINATIM_URL, params={
        'q': query, 'format': 'json', 'limit': 5, 'countrycodes': 'us'
    }, timeout=10)
    results = r.json()
    if not results:
        return None
    
    # Prefer natural features, parks, water bodies over residential areas
    def score(res):
        cls = res.get('class', '')
        typ = res.get('type', '')
        if cls == 'natural' or cls == 'water' or typ in ('lake', 'peak', 'mountain', 'river'):
            return 0
        if cls == 'leisure' or 'park' in typ or 'forest' in typ:
            return 1
        if cls == 'boundary' and 'national' in res.get('display_name', '').lower():
            return 2
        if cls in ('place',) and typ in ('city', 'town', 'village'):
            return 3
        return 5
    
    best = min(results, key=score)
    
    bbox = best.get('boundingbox', [])
    return {
        'lat': float(best['lat']),
        'lon': float(best['lon']),
        'bbox': [float(b) for b in bbox] if len(bbox) == 4 else None
    }

_last_overpass_request = 0
OVERPASS_SERVERS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
]

def overpass_query(query):
    global _last_overpass_request
    cache_key = hashlib.md5(query.encode()).hexdigest()
    now = time.time()
    
    # Prune stale cache entries periodically
    if len(_overpass_cache) > 100:
        stale = [k for k, (ts, _) in _overpass_cache.items() if now - ts > CACHE_TTL]
        for k in stale:
            del _overpass_cache[k]
    
    if cache_key in _overpass_cache:
        ts, data = _overpass_cache[cache_key]
        if now - ts < CACHE_TTL:
            return data
    
    # Rate limit: wait at least 1.5s between requests
    elapsed = now - _last_overpass_request
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    
    # Try multiple servers
    last_err = None
    for server in OVERPASS_SERVERS:
        try:
            _last_overpass_request = time.time()
            r = http_session.post(server, data={'data': query}, timeout=30)
            if r.status_code == 429:
                last_err = Exception(f'Rate limited by {server}')
                time.sleep(2)
                continue
            r.raise_for_status()
            data = r.json()
            _overpass_cache[cache_key] = (time.time(), data)
            return data
        except Exception as e:
            last_err = e
            continue
    
    raise last_err or Exception('All Overpass servers failed')

def parse_osm_trails(data):
    """Parse Overpass response into trail list."""
    results = []
    seen = set()
    for el in data.get('elements', []):
        osm_type = el.get('type')  # way or relation
        osm_id = el.get('id')
        tags = el.get('tags', {})
        name = tags.get('name', '')
        if not name:
            continue
        key = f"{osm_type}:{osm_id}"
        if key in seen:
            continue
        seen.add(key)
        
        # Get center coords
        lat = lon = None
        if 'center' in el:
            lat = el['center'].get('lat')
            lon = el['center'].get('lon')
        elif 'lat' in el:
            lat = el.get('lat')
            lon = el.get('lon')
        
        results.append({
            'id': f"{osm_type}:{osm_id}",
            'osm_type': osm_type,
            'osm_id': osm_id,
            'name': name,
            'lat': lat,
            'lon': lon,
            'difficulty': tags.get('sac_scale', ''),
            'surface': tags.get('surface', ''),
            'distance': tags.get('distance', ''),
            'desc': tags.get('description', tags.get('note', ''))[:200] if tags.get('description') or tags.get('note') else '',
        })
    return results

# --- Pages ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/explore')
def explore():
    return render_template('explore.html')

@app.route('/report')
def report():
    return render_template('report.html')

@app.route('/trail/<osm_type>/<int:osm_id>')
def trail_detail(osm_type, osm_id):
    if osm_type not in ('way', 'relation'):
        return 'Invalid trail type', 404
    return render_template('trail.html', osm_type=osm_type, osm_id=osm_id)

# Keep old route for backwards compat
@app.route('/trail/<facility_id>')
def trail_detail_legacy(facility_id):
    return render_template('trail.html', osm_type='legacy', osm_id=facility_id)

# --- API ---
_autocomplete_cache = {}
AUTOCOMPLETE_TTL = 300  # 5 minutes

@app.route('/api/autocomplete')
def api_autocomplete():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    
    ac_key = q.lower()
    now = time.time()
    if ac_key in _autocomplete_cache:
        ts, data = _autocomplete_cache[ac_key]
        if now - ts < AUTOCOMPLETE_TTL:
            return jsonify(data)
    
    results = []
    
    # 1. Query Nominatim for place suggestions — sorted by importance (popularity)
    try:
        r = http_session.get(NOMINATIM_URL, params={
            'q': q, 'format': 'json', 'limit': 15, 'countrycodes': 'us',
            'addressdetails': 1
        }, timeout=5)
        places = r.json()
        
        # Sort by importance (higher = more popular/well-known) and prefer outdoor features
        def place_score(p):
            importance = float(p.get('importance', 0))
            cls = p.get('class', '')
            typ = p.get('type', '')
            # Boost natural features, parks, and trails
            if cls == 'natural' or typ in ('peak', 'mountain', 'lake', 'river', 'valley'):
                importance += 0.3
            elif cls == 'leisure' or 'park' in typ or 'forest' in typ:
                importance += 0.2
            elif cls == 'highway' and typ in ('path', 'footway', 'track'):
                importance += 0.25
            elif cls == 'boundary' and 'national' in p.get('display_name', '').lower():
                importance += 0.2
            return -importance  # negative for ascending sort
        
        places.sort(key=place_score)
        
        for place in places[:10]:
            display = place.get('display_name', '')
            parts = display.split(',')
            name = parts[0].strip()
            context = ', '.join(p.strip() for p in parts[1:3]) if len(parts) > 1 else ''
            cls = place.get('class', '')
            typ = place.get('type', '')
            item_type = 'trail' if cls == 'highway' and typ in ('path', 'footway', 'track') else 'area'
            results.append({
                'type': item_type,
                'name': name,
                'context': context,
                'osm_type': place.get('osm_type'),
                'osm_id': place.get('osm_id'),
                'lat': place.get('lat'),
                'lon': place.get('lon'),
            })
    except Exception:
        pass
    
    # 2. Also check cached Overpass results for matching trail names
    for ck, (ts, cdata) in list(_overpass_cache.items()):
        if now - ts > CACHE_TTL:
            continue
        for el in cdata.get('elements', []):
            tags = el.get('tags', {})
            name = tags.get('name', '')
            if name and q.lower() in name.lower():
                osm_type = el.get('type')
                osm_id = el.get('id')
                dup = any(r.get('osm_id') == osm_id and r.get('osm_type') == osm_type for r in results)
                if not dup:
                    results.insert(0, {
                        'type': 'trail',
                        'name': name,
                        'context': tags.get('description', '')[:80] if tags.get('description') else '',
                        'osm_type': osm_type,
                        'osm_id': osm_id,
                    })
    
    _autocomplete_cache[ac_key] = (now, results)
    return jsonify(results)

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '').strip()
    state = request.args.get('state', '').strip()
    lat = request.args.get('lat', '').strip()
    lon = request.args.get('lon', '').strip()
    bbox = request.args.get('bbox', '').strip()  # south,west,north,east
    
    try:
        if bbox:
            # Search by map bounding box
            parts = bbox.split(',')
            if len(parts) == 4:
                s, w, n, e = parts
                query = f'''[out:json][timeout:25];
(
  way["highway"~"path|footway"]["name"]({s},{w},{n},{e});
  relation["route"="hiking"]["name"]({s},{w},{n},{e});
);
out center tags 100;'''
                return jsonify(parse_osm_trails(overpass_query(query)))
        elif lat and lon:
            # Near me search
            query = f'''[out:json][timeout:25];
(
  way["highway"~"path|footway"]["name"](around:8000,{lat},{lon});
  relation["route"="hiking"]["name"](around:8000,{lat},{lon});
);
out center tags;'''
        elif state and state in STATE_NAMES:
            state_name = STATE_NAMES[state]
            query = f'''[out:json][timeout:25];
area["name"="{state_name}"]["admin_level"="4"]->.searchArea;
(
  relation["route"="hiking"]["name"](area.searchArea);
);
out center tags 50;'''
        elif q:
            # Geocode the query to get a bounding box, then search trails in that area
            geo = geocode(q)
            results = []
            
            if geo and geo['bbox']:
                south, north, west, east = geo['bbox']
                # Ensure minimum bbox size (~20km)
                lat_span = north - south
                lon_span = east - west
                if lat_span < 0.2:
                    pad_lat = (0.2 - lat_span) / 2
                    south -= pad_lat
                    north += pad_lat
                if lon_span < 0.2:
                    pad_lon = (0.2 - lon_span) / 2
                    west -= pad_lon
                    east += pad_lon
                bbox_str = f"{south},{west},{north},{east}"
                bbox_query = f'''[out:json][timeout:25];
(
  way["highway"~"path|footway"]["name"]({bbox_str});
  relation["route"="hiking"]["name"]({bbox_str});
);
out center tags 100;'''
                try:
                    results = parse_osm_trails(overpass_query(bbox_query))
                except Exception:
                    pass
            elif geo:
                # No bbox, use around search
                around_query = f'''[out:json][timeout:25];
(
  way["highway"~"path|footway"]["name"](around:15000,{geo['lat']},{geo['lon']});
  relation["route"="hiking"]["name"](around:15000,{geo['lat']},{geo['lon']});
);
out center tags 100;'''
                try:
                    results = parse_osm_trails(overpass_query(around_query))
                except Exception:
                    pass
            
            # Also search hiking relations by name globally (fast)
            try:
                name_query = f'''[out:json][timeout:10];
relation["route"="hiking"]["name"~"{q}",i];
out center tags 20;'''
                name_results = parse_osm_trails(overpass_query(name_query))
                seen_ids = {r['id'] for r in results}
                for r in name_results:
                    if r['id'] not in seen_ids:
                        results.append(r)
            except Exception:
                pass
            
            return jsonify(results[:100])
        else:
            return jsonify([])
        
        results = parse_osm_trails(overpass_query(query))
        return jsonify(results[:100])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trail/<osm_type>/<int:osm_id>')
def api_trail(osm_type, osm_id):
    if osm_type not in ('way', 'relation'):
        return jsonify({'error': 'Invalid type'}), 400
    
    # Check cache first
    cache_key = f"trail:{osm_type}:{osm_id}"
    cached = cached_response(cache_key, ttl=CACHE_TTL)
    if cached:
        return jsonify(cached)
    
    try:
        query = f'''[out:json][timeout:25];
{osm_type}({osm_id});
out geom tags;'''
        data = overpass_query(query)
        elements = data.get('elements', [])
        if not elements:
            return jsonify({'error': 'Trail not found'}), 404
        
        el = elements[0]
        tags = el.get('tags', {})
        
        # Extract geometry
        geometry = []
        if osm_type == 'way' and 'geometry' in el:
            geometry = [{'lat': p['lat'], 'lon': p['lon']} for p in el['geometry']]
        elif osm_type == 'relation' and 'members' in el:
            for member in el.get('members', []):
                if member.get('type') == 'way' and 'geometry' in member:
                    geometry.extend([{'lat': p['lat'], 'lon': p['lon']} for p in member['geometry']])
        
        # Calculate center from geometry
        lat = lon = None
        if geometry:
            lat = sum(p['lat'] for p in geometry) / len(geometry)
            lon = sum(p['lon'] for p in geometry) / len(geometry)
        elif 'center' in el:
            lat = el['center']['lat']
            lon = el['center']['lon']
        
        # Calculate approximate distance in km from geometry
        distance_km = None
        if len(geometry) > 1:
            total = 0
            for i in range(len(geometry) - 1):
                lat1, lon1 = radians(geometry[i]['lat']), radians(geometry[i]['lon'])
                lat2, lon2 = radians(geometry[i+1]['lat']), radians(geometry[i+1]['lon'])
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
                total += 6371 * 2 * atan2(sqrt(a), sqrt(1-a))
            distance_km = round(total, 1)
        
        result = {
            'osm_type': osm_type,
            'osm_id': osm_id,
            'name': tags.get('name', f'{osm_type}/{osm_id}'),
            'desc': tags.get('description', tags.get('note', '')),
            'lat': lat,
            'lon': lon,
            'geometry': geometry,
            'difficulty': tags.get('sac_scale', ''),
            'surface': tags.get('surface', ''),
            'distance_km': distance_km,
            'distance_tag': tags.get('distance', ''),
            'access': tags.get('access', ''),
            'wheelchair': tags.get('wheelchair', ''),
            'network': tags.get('network', ''),
            'operator': tags.get('operator', ''),
            'website': tags.get('website', tags.get('url', '')),
        }
        cache_response(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/elevation')
def api_elevation():
    """Get elevation profile for a list of lat/lon points using Open-Meteo Elevation API."""
    lats = request.args.get('lats', '')
    lons = request.args.get('lons', '')
    if not lats or not lons:
        return jsonify({'error': 'Missing lats/lons'}), 400
    
    try:
        # Open-Meteo accepts up to ~100 points per request
        lat_list = lats.split(',')
        lon_list = lons.split(',')
        
        elevations = []
        # Batch in groups of 100
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

_weather_cache = {}
WEATHER_CACHE_TTL = 900  # 15 minutes

@app.route('/api/weather/grid')
def api_weather_grid():
    """Batch weather inference for multiple lat/lon points. Expects lats=a,b,c&lons=a,b,c"""
    lats = request.args.get('lats', '')
    lons = request.args.get('lons', '')
    if not lats or not lons:
        return jsonify({'error': 'Missing lats/lons'}), 400
    
    lat_list = lats.split(',')
    lon_list = lons.split(',')
    if len(lat_list) != len(lon_list):
        return jsonify({'error': 'Mismatched lats/lons'}), 400
    
    now = time.time()
    results = []
    uncached_indices = []
    
    # Check cache first
    for i in range(len(lat_list)):
        key = f"grid:{float(lat_list[i]):.2f},{float(lon_list[i]):.2f}"
        if key in _weather_cache and now - _weather_cache[key][0] < WEATHER_CACHE_TTL:
            results.append(_weather_cache[key][1])
        else:
            results.append(None)
            uncached_indices.append(i)
    
    # Fetch uncached in parallel using Open-Meteo multi-location
    if uncached_indices:
        # Open-Meteo supports comma-separated coords for batch
        batch_lats = ','.join(lat_list[i] for i in uncached_indices)
        batch_lons = ','.join(lon_list[i] for i in uncached_indices)
        try:
            r = http_session.get('https://api.open-meteo.com/v1/forecast', params={
                'latitude': batch_lats,
                'longitude': batch_lons,
                'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,rain_sum',
                'past_days': 7,
                'forecast_days': 1,
                'timezone': 'auto'
            }, timeout=15)
            data = r.json()
            
            # Multi returns list, single returns dict
            if not isinstance(data, list):
                data = [data]
            
            for j, d in enumerate(data):
                idx = uncached_indices[j]
                daily = d.get('daily', {})
                total_snow = sum(v for v in (daily.get('snowfall_sum') or []) if v)
                total_rain = sum(v for v in (daily.get('rain_sum') or []) if v)
                total_precip = sum(v for v in (daily.get('precipitation_sum') or []) if v)
                min_temps = [v for v in (daily.get('temperature_2m_min') or []) if v is not None]
                avg_min = sum(min_temps) / len(min_temps) if min_temps else 0
                
                reasons = []
                condition = 'clear'
                if total_snow > 5:
                    condition = 'snowy'
                    reasons.append(f'{total_snow:.1f}cm snow in 7 days')
                elif total_snow > 0.5:
                    if avg_min < 0:
                        condition = 'icy'
                        reasons.append(f'{total_snow:.1f}cm snow + freezing temps')
                    else:
                        condition = 'snowy'
                        reasons.append(f'{total_snow:.1f}cm snow in 7 days')
                if total_rain > 20:
                    if condition == 'clear':
                        condition = 'muddy'
                    reasons.append(f'{total_rain:.1f}mm rain — expect mud')
                elif total_rain > 5:
                    if condition == 'clear':
                        condition = 'wet'
                    reasons.append(f'{total_rain:.1f}mm rain in 7 days')
                if condition == 'clear' and total_precip < 2:
                    condition = 'dry'
                    reasons.append('Minimal precipitation — trails likely dry')
                if not reasons:
                    reasons.append('Conditions look good')
                
                inf = {'condition': condition, 'reasons': reasons}
                results[idx] = inf
                key = f"grid:{float(lat_list[idx]):.2f},{float(lon_list[idx]):.2f}"
                _weather_cache[key] = (now, inf)
        except Exception:
            for j in uncached_indices:
                if results[j] is None:
                    results[j] = {'condition': 'clear', 'reasons': ['Unable to fetch weather']}
    
    return jsonify(results)

@app.route('/api/weather/batch')
def api_weather_batch():
    locations = request.args.get('locations', '')
    if not locations:
        return jsonify([])
    pairs = [l.split(',') for l in locations.split('|') if ',' in l]
    now = time.time()
    results = []
    # Open-Meteo supports comma-separated lat/lon for multi-location
    uncached = []
    for lat, lon in pairs[:20]:
        key = f"{float(lat):.2f},{float(lon):.2f}"
        if key in _weather_cache and now - _weather_cache[key][0] < WEATHER_CACHE_TTL:
            results.append(_weather_cache[key][1])
        else:
            uncached.append((lat, lon, len(results)))
            results.append(None)
    
    if uncached:
        try:
            lats = ','.join(u[0] for u in uncached)
            lons = ','.join(u[1] for u in uncached)
            r = http_session.get('https://api.open-meteo.com/v1/forecast', params={
                'latitude': lats, 'longitude': lons,
                'current_weather': 'true'
            }, timeout=10)
            data = r.json()
            # Multi-location returns list, single returns dict
            if isinstance(data, list):
                for i, d in enumerate(data):
                    w = d.get('current_weather', {})
                    result = {'lat': float(uncached[i][0]), 'lon': float(uncached[i][1]), 'temp_c': w.get('temperature'), 'windspeed': w.get('windspeed'), 'weathercode': w.get('weathercode')}
                    results[uncached[i][2]] = result
                    key = f"{float(uncached[i][0]):.2f},{float(uncached[i][1]):.2f}"
                    _weather_cache[key] = (now, result)
            else:
                w = data.get('current_weather', {})
                result = {'lat': float(uncached[0][0]), 'lon': float(uncached[0][1]), 'temp_c': w.get('temperature'), 'windspeed': w.get('windspeed'), 'weathercode': w.get('weathercode')}
                results[uncached[0][2]] = result
                key = f"{float(uncached[0][0]):.2f},{float(uncached[0][1]):.2f}"
                _weather_cache[key] = (now, result)
        except Exception:
            for u in uncached:
                if results[u[2]] is None:
                    results[u[2]] = {'lat': float(u[0]), 'lon': float(u[1]), 'error': True}
    
    results = [r for r in results if r is not None]
    return jsonify(results)

@app.route('/api/weather/history')
def api_weather_history():
    lat = request.args.get('lat')
    lon = request.args.get('lon')
    if not lat or not lon:
        return jsonify({'error': 'lat/lon required'}), 400
    
    # Cache weather for 30 min (doesn't change fast)
    wh_key = f"wxh:{float(lat):.2f},{float(lon):.2f}"
    cached = cached_response(wh_key, ttl=1800)
    if cached:
        return jsonify(cached)
    
    try:
        r = http_session.get('https://api.open-meteo.com/v1/forecast', params={
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
        
        badge = 'green'
        if condition in ('wet', 'muddy'):
            badge = 'yellow'
        elif condition in ('snowy', 'icy'):
            badge = 'red'
        
        wx_result = {
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
        }
        cache_response(wh_key, wx_result, ttl=1800)
        return jsonify(wx_result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Reports use facility_id which is now "way:123" or "relation:456"
@app.route('/api/trail/<path:facility_id>/reports')
def get_reports(facility_id):
    db = get_db()
    reports = db.execute('SELECT * FROM reports WHERE facility_id=? ORDER BY created_at DESC', (facility_id,)).fetchall()
    return jsonify([dict(r) for r in reports])

@app.route('/api/trail/<path:facility_id>/reports', methods=['POST'])
def add_report(facility_id):
    data = request.json
    db = get_db()
    db.execute('''INSERT INTO reports (facility_id, trail_name, trail_condition, trail_surface, weather, road_access, parking, issues, general_notes, date_visited) 
                  VALUES (?,?,?,?,?,?,?,?,?,?)''',
               (facility_id, data.get('trail_name'), data.get('trail_condition'), data.get('trail_surface'),
                data.get('weather'), data.get('road_access'), data.get('parking'), data.get('issues'),
                data.get('general_notes'), data.get('date_visited')))
    db.commit()
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
                return jsonify({'error': 'Already voted'}), 400
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
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})

# Initialize DB on import (works with both gunicorn and direct run)
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8095, debug=False, threaded=True)

from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_compress import Compress
import requests
import hashlib
import time
import os
import logging
from math import radians, sin, cos, sqrt, atan2
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from database import get_db, init_db

logger = logging.getLogger(__name__)

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

# USGS National Map Trails API
USGS_TRAILS_URL = 'https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer/37/query'
usgs_session = requests.Session()
usgs_session.headers.update({'User-Agent': 'TrailCondish/1.0'})
_usgs_cache = {}
USGS_CACHE_TTL = 600  # 10 minutes
_usgs_executor = ThreadPoolExecutor(max_workers=2)

def _usgs_cache_key(params):
    return hashlib.md5(str(sorted(params.items())).encode()).hexdigest()

def usgs_query_bbox(south, west, north, east, trail_type='all', return_geometry=False, max_results=500):
    """Query USGS trails API for a bounding box. Returns list of trail dicts."""
    params = {
        'geometry': f'{west},{south},{east},{north}',
        'geometryType': 'esriGeometryEnvelope',
        'inSR': '4326',
        'outSR': '4326',
        'outFields': 'name,trailtype,hikerpedestrian,bicycle,packsaddle,lengthmiles,primarytrailmaintainer,nationaltraildesignation',
        'returnGeometry': 'true' if return_geometry else 'false',
        'resultRecordCount': min(max_results, 2000),
        'f': 'geojson' if return_geometry else 'json',
    }
    
    # Don't filter by activity type in USGS — many trails have null for activity fields
    # We'll use the data for enrichment regardless
    params['where'] = "name IS NOT NULL AND name <> ''"
    
    cache_key = _usgs_cache_key(params)
    now = time.time()
    
    # Check cache
    if cache_key in _usgs_cache:
        ts, data = _usgs_cache[cache_key]
        if now - ts < USGS_CACHE_TTL:
            return data
    
    # Prune stale cache
    if len(_usgs_cache) > 200:
        stale = [k for k, (ts, _) in _usgs_cache.items() if now - ts > USGS_CACHE_TTL]
        for k in stale:
            del _usgs_cache[k]
    
    try:
        r = usgs_session.get(USGS_TRAILS_URL, params=params, timeout=5)
        r.raise_for_status()
        raw = r.json()
        
        trails = []
        if return_geometry:
            # GeoJSON format
            for feat in raw.get('features', []):
                props = feat.get('properties', {})
                geom = feat.get('geometry', {})
                t = _parse_usgs_trail(props)
                if geom and geom.get('paths'):
                    t['usgs_geometry'] = geom['paths']
                elif geom and geom.get('coordinates'):
                    t['usgs_geometry'] = geom['coordinates']
                trails.append(t)
        else:
            for feat in raw.get('features', []):
                props = feat.get('attributes', {})
                trails.append(_parse_usgs_trail(props))
        
        _usgs_cache[cache_key] = (now, trails)
        return trails
    except Exception as e:
        logger.warning(f'USGS query failed: {e}')
        return []

def _parse_usgs_trail(props):
    """Parse USGS trail attributes into a normalized dict."""
    activities = []
    if props.get('hikerpedestrian') == 'Yes':
        activities.append('hiking')
    if props.get('bicycle') == 'Yes':
        activities.append('biking')
    if props.get('packsaddle') == 'Yes':
        activities.append('horse')
    
    length_miles = props.get('lengthmiles')
    if length_miles is not None:
        try:
            length_miles = round(float(length_miles), 1)
        except (ValueError, TypeError):
            length_miles = None
    
    return {
        'name': (props.get('name') or '').strip(),
        'usgs_trail_type': props.get('trailtype', ''),
        'length_miles': length_miles,
        'activities': activities,
        'maintainer': (props.get('primarytrailmaintainer') or '').strip(),
        'designation': (props.get('nationaltraildesignation') or '').strip(),
        'source': 'usgs',
    }

def _haversine_km_simple(lat1, lon1, lat2, lon2):
    """Quick haversine distance in km."""
    rlat1, rlon1, rlat2, rlon2 = radians(lat1), radians(lon1), radians(lat2), radians(lon2)
    dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
    a = sin(dlat/2)**2 + cos(rlat1)*cos(rlat2)*sin(dlon/2)**2
    return 6371 * 2 * atan2(sqrt(a), sqrt(1-a))

def merge_usgs_into_osm(osm_trails, usgs_trails):
    """Merge USGS trail data into OSM results. Enrich matching trails, add unique USGS ones."""
    if not usgs_trails:
        return osm_trails
    
    # Build name-based index for OSM trails
    osm_by_name = {}
    for t in osm_trails:
        norm = t.get('name', '').strip().lower()
        if norm:
            if norm not in osm_by_name:
                osm_by_name[norm] = []
            osm_by_name[norm].append(t)
    
    matched_usgs = set()
    
    # Group USGS trails by name, pick best (longest) for each name
    usgs_by_name = {}
    for ut in usgs_trails:
        uname = ut['name'].strip().lower()
        if not uname:
            continue
        if uname not in usgs_by_name or (ut.get('length_miles') or 0) > (usgs_by_name[uname].get('length_miles') or 0):
            usgs_by_name[uname] = ut
    
    for uname, ut in usgs_by_name.items():
        # Try exact name match first
        if uname in osm_by_name:
            for osm_t in osm_by_name[uname]:
                _enrich_osm_with_usgs(osm_t, ut)
            continue
        
        # Try substring match (USGS name in OSM name or vice versa)
        for norm, osm_list in osm_by_name.items():
            if len(uname) > 4 and len(norm) > 4 and (uname in norm or norm in uname):
                for osm_t in osm_list:
                    _enrich_osm_with_usgs(osm_t, ut)
                break
    
    # Don't add unmatched USGS trails as standalone results (they lack coordinates for map display)
    return osm_trails

def _enrich_osm_with_usgs(osm_trail, usgs_trail):
    """Add USGS metadata to an OSM trail dict."""
    if usgs_trail.get('length_miles'):
        osm_trail['length_miles'] = usgs_trail['length_miles']
    if usgs_trail.get('activities'):
        osm_trail['activities'] = usgs_trail['activities']
    if usgs_trail.get('maintainer'):
        osm_trail['maintainer'] = usgs_trail['maintainer']
    if usgs_trail.get('usgs_trail_type'):
        osm_trail['usgs_trail_type'] = usgs_trail['usgs_trail_type']
    if usgs_trail.get('designation'):
        osm_trail['designation'] = usgs_trail['designation']
    osm_trail['usgs_enriched'] = True

def fetch_usgs_for_bbox(south, west, north, east, trail_type='all'):
    """Non-blocking USGS fetch using thread pool. Returns future."""
    return _usgs_executor.submit(usgs_query_bbox, south, west, north, east, trail_type)

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
    """Parse Overpass response into trail list, merging same-name way segments."""
    relations = []
    way_groups = {}  # name -> list of way elements
    seen_rel = set()
    
    for el in data.get('elements', []):
        osm_type = el.get('type')
        osm_id = el.get('id')
        tags = el.get('tags', {})
        name = tags.get('name', '')
        if not name:
            continue
        
        if osm_type == 'relation':
            key = f"relation:{osm_id}"
            if key not in seen_rel:
                seen_rel.add(key)
                lat = lon = None
                if 'center' in el:
                    lat, lon = el['center'].get('lat'), el['center'].get('lon')
                relations.append({
                    'id': key, 'osm_type': 'relation', 'osm_id': osm_id,
                    'name': name, 'lat': lat, 'lon': lon,
                    'difficulty': tags.get('sac_scale', ''),
                    'surface': tags.get('surface', ''),
                    'distance': tags.get('distance', ''),
                    'desc': (tags.get('description') or tags.get('note', ''))[:200],
                })
        elif osm_type == 'way':
            norm = name.strip().lower()
            way_el = {'id': osm_id, 'lat': None, 'lon': None, 'tags': tags, 'name': name}
            if 'center' in el:
                way_el['lat'] = el['center'].get('lat')
                way_el['lon'] = el['center'].get('lon')
            if norm not in way_groups:
                way_groups[norm] = []
            way_groups[norm].append(way_el)
    
    # Names already covered by relations — skip those way groups
    rel_names = {r['name'].strip().lower() for r in relations}
    
    # Proximity-cluster same-name ways (only merge if within ~5km of each other)
    MAX_MERGE_KM = 5.0
    
    def _haversine_km(lat1, lon1, lat2, lon2):
        """Quick haversine distance in km."""
        from math import radians as rad, sin, cos, sqrt, atan2
        rlat1, rlon1, rlat2, rlon2 = rad(lat1), rad(lon1), rad(lat2), rad(lon2)
        dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
        a = sin(dlat/2)**2 + cos(rlat1)*cos(rlat2)*sin(dlon/2)**2
        return 6371 * 2 * atan2(sqrt(a), sqrt(1-a))
    
    def _cluster_ways(ways):
        """Group ways into proximity clusters. Each cluster becomes one result."""
        clusters = []
        for w in ways:
            if w['lat'] is None or w['lon'] is None:
                # No coords — put in own cluster
                clusters.append([w])
                continue
            merged = False
            for cluster in clusters:
                # Check if this way is near any way in the cluster
                for cw in cluster:
                    if cw['lat'] is not None and cw['lon'] is not None:
                        if _haversine_km(w['lat'], w['lon'], cw['lat'], cw['lon']) <= MAX_MERGE_KM:
                            cluster.append(w)
                            merged = True
                            break
                if merged:
                    break
            if not merged:
                clusters.append([w])
        return clusters
    
    results = list(relations)
    for norm, ways in way_groups.items():
        if norm in rel_names:
            continue
        clusters = _cluster_ways(ways)
        for cluster in clusters:
            rep = cluster[0]
            tags = rep['tags']
            way_ids = [w['id'] for w in cluster]
            # Use first way's start point (trailhead) not average center
            lat = rep.get('lat')
            lon = rep.get('lon')
            
            entry = {
                'id': f"way:{rep['id']}",
                'osm_type': 'way', 'osm_id': rep['id'],
                'name': rep['name'], 'lat': lat, 'lon': lon,
                'difficulty': tags.get('sac_scale', ''),
                'surface': tags.get('surface', ''),
                'distance': tags.get('distance', ''),
                'desc': (tags.get('description') or tags.get('note', ''))[:200],
            }
            if len(way_ids) > 1:
                entry['way_ids'] = way_ids
            results.append(entry)
    
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
    
    # 1. Query Photon (Komoot) for autocomplete — much better partial matching than Nominatim
    try:
        r = http_session.get('https://photon.komoot.io/api/', params={
            'q': q, 'limit': 15, 'lang': 'en',
            'bbox': '-125,24,-66,50',  # Continental US
        }, timeout=5)
        features = r.json().get('features', [])
        
        # Score and sort: prefer outdoor/natural features
        def place_score(f):
            props = f.get('properties', {})
            osm_value = props.get('osm_value', '')
            osm_key = props.get('osm_key', '')
            score = 0
            # Boost natural features, parks, trails, and places (towns/cities)
            if osm_key == 'place' and osm_value in ('city', 'town', 'village', 'hamlet', 'suburb', 'borough'):
                score += 5  # Towns/cities should always rank highest for location searches
            elif osm_key == 'natural' or osm_value in ('peak', 'mountain', 'lake', 'river', 'valley', 'water'):
                score += 3
            elif osm_key == 'leisure' or 'park' in osm_value or 'forest' in osm_value:
                score += 2
            elif osm_key == 'highway' and osm_value in ('path', 'footway', 'track'):
                score += 3
            elif osm_key == 'boundary' and 'national' in props.get('name', '').lower():
                score += 2
            # Boost by name match quality
            name = props.get('name', '').lower()
            if name.startswith(q.lower()):
                score += 2
            elif q.lower() in name:
                score += 1
            return -score
        
        features.sort(key=place_score)
        
        for f in features[:10]:
            props = f.get('properties', {})
            coords = f.get('geometry', {}).get('coordinates', [])
            name = props.get('name', '')
            if not name:
                continue
            
            # Build context from city/state
            ctx_parts = []
            if props.get('city'):
                ctx_parts.append(props['city'])
            elif props.get('county'):
                ctx_parts.append(props['county'])
            if props.get('state'):
                ctx_parts.append(props['state'])
            context = ', '.join(ctx_parts)
            
            osm_key = props.get('osm_key', '')
            osm_value = props.get('osm_value', '')
            item_type = 'trail' if osm_key == 'highway' and osm_value in ('path', 'footway', 'track') else 'area'
            
            results.append({
                'type': item_type,
                'name': name,
                'context': context,
                'osm_type': props.get('osm_type'),
                'osm_id': props.get('osm_id'),
                'lat': str(coords[1]) if len(coords) > 1 else '',
                'lon': str(coords[0]) if coords else '',
            })
    except Exception:
        # Fallback to Nominatim if Photon fails
        try:
            r = http_session.get(NOMINATIM_URL, params={
                'q': q, 'format': 'json', 'limit': 10, 'countrycodes': 'us'
            }, timeout=5)
            for place in r.json():
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

TRAIL_TYPE_QUERIES = {
    'all': {
        'ways': '["highway"~"path|footway|cycleway|bridleway"]',
        'rels': '["route"~"hiking|bicycle|horse"]',
    },
    'hiking': {
        'ways': '["highway"~"path|footway"]',
        'rels': '["route"="hiking"]',
    },
    'biking': {
        'ways': '["highway"~"cycleway|path"]["bicycle"!="no"]',
        'rels': '["route"="bicycle"]',
    },
    'paved': {
        'ways': '["highway"~"path|footway|cycleway"]["surface"~"paved|asphalt|concrete|compacted"]',
        'rels': '["route"~"hiking|bicycle"]["surface"~"paved|asphalt|concrete|compacted"]',
    },
    'horse': {
        'ways': '["highway"~"bridleway|path"]["horse"!="no"]',
        'rels': '["route"="horse"]',
    },
}

def get_trail_query_parts(trail_type):
    return TRAIL_TYPE_QUERIES.get(trail_type, TRAIL_TYPE_QUERIES['all'])

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '').strip()
    state = request.args.get('state', '').strip()
    lat = request.args.get('lat', '').strip()
    lon = request.args.get('lon', '').strip()
    bbox = request.args.get('bbox', '').strip()
    trail_type = request.args.get('type', 'all').strip()
    tp = get_trail_query_parts(trail_type)
    usgs_future = None
    
    try:
        if bbox:
            parts = bbox.split(',')
            if len(parts) == 4:
                s, w, n, e = parts
                # Fire USGS query in parallel
                usgs_future = fetch_usgs_for_bbox(float(s), float(w), float(n), float(e), trail_type)
                query = f'''[out:json][timeout:25];
(
  way{tp['ways']}["name"]({s},{w},{n},{e});
  relation{tp['rels']}["name"]({s},{w},{n},{e});
);
out center tags 100;'''
                results = parse_osm_trails(overpass_query(query))
                # Merge USGS (wait up to 3s, don't block if slow)
                try:
                    usgs_trails = usgs_future.result(timeout=3)
                    results = merge_usgs_into_osm(results, usgs_trails)
                except Exception:
                    pass
                return jsonify(results)
        elif lat and lon:
            usgs_future = fetch_usgs_for_bbox(
                float(lat) - 0.1, float(lon) - 0.1,
                float(lat) + 0.1, float(lon) + 0.1, trail_type)
            query = f'''[out:json][timeout:25];
(
  way{tp['ways']}["name"](around:8000,{lat},{lon});
  relation{tp['rels']}["name"](around:8000,{lat},{lon});
);
out center tags;'''
        elif state and state in STATE_NAMES:
            state_name = STATE_NAMES[state]
            query = f'''[out:json][timeout:25];
area["name"="{state_name}"]["admin_level"="4"]->.searchArea;
(
  relation{tp['rels']}["name"](area.searchArea);
);
out center tags 50;'''
        elif q:
            geo = geocode(q)
            results = []
            usgs_future = None
            
            if geo and geo['bbox']:
                south, north, west, east = geo['bbox']
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
                # Fire USGS in parallel
                usgs_future = fetch_usgs_for_bbox(south, west, north, east, trail_type)
                bbox_str = f"{south},{west},{north},{east}"
                bbox_query = f'''[out:json][timeout:25];
(
  way{tp['ways']}["name"]({bbox_str});
  relation{tp['rels']}["name"]({bbox_str});
);
out center tags 100;'''
                try:
                    results = parse_osm_trails(overpass_query(bbox_query))
                except Exception:
                    pass
            elif geo:
                # Fire USGS for area around the point
                usgs_future = fetch_usgs_for_bbox(
                    geo['lat'] - 0.15, geo['lon'] - 0.15,
                    geo['lat'] + 0.15, geo['lon'] + 0.15, trail_type)
                around_query = f'''[out:json][timeout:25];
(
  way{tp['ways']}["name"](around:15000,{geo['lat']},{geo['lon']});
  relation{tp['rels']}["name"](around:15000,{geo['lat']},{geo['lon']});
);
out center tags 100;'''
                try:
                    results = parse_osm_trails(overpass_query(around_query))
                except Exception:
                    pass
            
            # Also search hiking relations by name globally (fast)
            try:
                name_query = f'''[out:json][timeout:10];
relation{tp['rels']}["name"~"{q}",i];
out center tags 20;'''
                name_results = parse_osm_trails(overpass_query(name_query))
                seen_ids = {r['id'] for r in results}
                for r in name_results:
                    if r['id'] not in seen_ids:
                        results.append(r)
            except Exception:
                pass
            
            # Merge USGS results (non-blocking, 3s timeout)
            if usgs_future:
                try:
                    usgs_trails = usgs_future.result(timeout=3)
                    results = merge_usgs_into_osm(results, usgs_trails)
                except Exception:
                    pass
            
            # Return geocoded center/bbox so client can fly to the location
            response = {'trails': results[:100]}
            if geo:
                response['center'] = {'lat': geo['lat'], 'lon': geo['lon']}
                if geo.get('bbox'):
                    response['bbox'] = geo['bbox']  # [south, north, west, east]
            return jsonify(response)
        else:
            return jsonify([])
        
        results = parse_osm_trails(overpass_query(query))
        # Merge USGS if future exists (lat/lon search)
        if usgs_future:
            try:
                usgs_trails = usgs_future.result(timeout=3)
                results = merge_usgs_into_osm(results, usgs_trails)
            except Exception:
                pass
        return jsonify(results[:100])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trail/<osm_type>/<int:osm_id>')
def api_trail(osm_type, osm_id):
    if osm_type not in ('way', 'relation'):
        return jsonify({'error': 'Invalid type'}), 400
    
    # For ways, check if we should fetch all same-name segments
    extra_way_ids = request.args.get('way_ids', '')
    
    # Check cache first
    cache_key = f"trail:{osm_type}:{osm_id}:{extra_way_ids}"
    cached = cached_response(cache_key, ttl=CACHE_TTL)
    if cached:
        return jsonify(cached)
    
    try:
        # If way_ids provided, fetch all segments at once
        if osm_type == 'way' and extra_way_ids:
            all_ids = [str(osm_id)] + [i.strip() for i in extra_way_ids.split(',') if i.strip()]
            all_ids = list(dict.fromkeys(all_ids))  # dedupe preserving order
            id_union = ''.join(f'way({wid});' for wid in all_ids)
            query = f'''[out:json][timeout:25];
({id_union});\nout geom tags;'''
        elif osm_type == 'relation':
            # Relations need special handling: get tags first, then expand members with geometry
            query = f'''[out:json][timeout:25];
relation({osm_id});out tags;
relation({osm_id});>;out geom;'''
        else:
            query = f'''[out:json][timeout:25];
{osm_type}({osm_id});
out geom tags;'''
        
        data = overpass_query(query)
        elements = data.get('elements', [])
        if not elements:
            return jsonify({'error': 'Trail not found'}), 404
        
        # For relations, find the relation element for tags and ways for geometry
        if osm_type == 'relation':
            rel_els = [e for e in elements if e.get('type') == 'relation']
            way_els = [e for e in elements if e.get('type') == 'way' and 'geometry' in e]
            el = rel_els[0] if rel_els else elements[0]
            tags = el.get('tags', {})
        else:
            el = elements[0]
            tags = el.get('tags', {})
            way_els = []
        
        # Extract geometry (combine all elements for multi-way)
        geometry = []
        if osm_type == 'way':
            for seg in elements:
                if 'geometry' in seg:
                    seg_pts = [{'lat': p['lat'], 'lon': p['lon']} for p in seg['geometry']]
                    geometry.extend(seg_pts)
        elif osm_type == 'relation':
            # Collect geometry from expanded member ways
            for way in way_els:
                if 'geometry' in way:
                    geometry.extend([{'lat': p['lat'], 'lon': p['lon']} for p in way['geometry']])
        
        # Use first point of geometry as trailhead/start position
        lat = lon = None
        if geometry:
            lat = geometry[0]['lat']
            lon = geometry[0]['lon']
        elif 'center' in el:
            lat = el['center']['lat']
            lon = el['center']['lon']
        
        # Deduplicate consecutive geometry points
        if len(geometry) > 1:
            deduped = [geometry[0]]
            for pt in geometry[1:]:
                if pt['lat'] != deduped[-1]['lat'] or pt['lon'] != deduped[-1]['lon']:
                    deduped.append(pt)
            geometry = deduped
        
        # Calculate approximate distance in km from geometry
        # Skip jumps > 500m between consecutive points (disconnected way segments)
        distance_km = None
        if len(geometry) > 1:
            total = 0
            for i in range(len(geometry) - 1):
                lat1, lon1 = radians(geometry[i]['lat']), radians(geometry[i]['lon'])
                lat2, lon2 = radians(geometry[i+1]['lat']), radians(geometry[i+1]['lon'])
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
                seg = 6371 * 2 * atan2(sqrt(a), sqrt(1-a))
                if seg < 0.5:  # Skip jumps > 500m (disconnected segments)
                    total += seg
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
        
        # === Trailhead detection + connected trail network ===
        trail_segments = []  # individual segments with their own geometry + stats
        access_trail = None  # "via" trail if no direct road/parking access
        has_trailhead = False
        
        if geometry and len(geometry) > 1:
            start_pt = geometry[0]
            end_pt = geometry[-1]
            
            try:
                # Check if start or end connects to road/parking/trailhead
                # Query for nearby road nodes, parking, or trailhead tags within ~50m
                th_query = f'''[out:json][timeout:10];
(
  way(around:50,{start_pt['lat']},{start_pt['lon']})["highway"~"^(residential|tertiary|secondary|primary|trunk|service|unclassified|track)$"];
  node(around:100,{start_pt['lat']},{start_pt['lon']})["amenity"="parking"];
  node(around:100,{start_pt['lat']},{start_pt['lon']})["highway"="trailhead"];
  way(around:50,{end_pt['lat']},{end_pt['lon']})["highway"~"^(residential|tertiary|secondary|primary|trunk|service|unclassified|track)$"];
  node(around:100,{end_pt['lat']},{end_pt['lon']})["amenity"="parking"];
  node(around:100,{end_pt['lat']},{end_pt['lon']})["highway"="trailhead"];
);
out tags 5;'''
                th_data = overpass_query(th_query)
                th_elements = th_data.get('elements', [])
                has_trailhead = len(th_elements) > 0
                
                # If no direct trailhead, find connecting trails at start/end points
                if not has_trailhead:
                    conn_query = f'''[out:json][timeout:10];
(
  way(around:30,{start_pt['lat']},{start_pt['lon']})["highway"~"^(path|footway|track|bridleway|cycleway)$"]["name"];
  way(around:30,{end_pt['lat']},{end_pt['lon']})["highway"~"^(path|footway|track|bridleway|cycleway)$"]["name"];
);
out center tags;'''
                    conn_data = overpass_query(conn_query)
                    conn_els = conn_data.get('elements', [])
                    trail_name_lower = result['name'].lower().strip()
                    for ce in conn_els:
                        ce_name = ce.get('tags', {}).get('name', '').strip()
                        if ce_name and ce_name.lower() != trail_name_lower:
                            ce_lat = ce.get('center', {}).get('lat')
                            ce_lon = ce.get('center', {}).get('lon')
                            access_trail = {
                                'name': ce_name,
                                'osm_type': 'way',
                                'osm_id': ce['id'],
                                'lat': ce_lat,
                                'lon': ce_lon,
                            }
                            break
            except Exception as e:
                pass  # Non-critical — don't fail the whole detail page
        
        # Build per-segment info for multi-way trails
        if osm_type == 'way' and extra_way_ids:
            for seg in elements:
                if 'geometry' not in seg:
                    continue
                seg_geo = [{'lat': p['lat'], 'lon': p['lon']} for p in seg['geometry']]
                seg_name = seg.get('tags', {}).get('name', f"Segment {seg['id']}")
                seg_dist = 0
                for i in range(len(seg_geo) - 1):
                    lat1, lon1 = radians(seg_geo[i]['lat']), radians(seg_geo[i]['lon'])
                    lat2, lon2 = radians(seg_geo[i+1]['lat']), radians(seg_geo[i+1]['lon'])
                    dlat = lat2 - lat1
                    dlon = lon2 - lon1
                    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
                    s = 6371 * 2 * atan2(sqrt(a), sqrt(1-a))
                    if s < 0.5:
                        seg_dist += s
                trail_segments.append({
                    'osm_id': seg['id'],
                    'name': seg_name,
                    'surface': seg.get('tags', {}).get('surface', ''),
                    'difficulty': seg.get('tags', {}).get('sac_scale', ''),
                    'distance_km': round(seg_dist, 2),
                    'distance_mi': round(seg_dist * 0.621371, 2),
                    'geometry': seg_geo,
                })
        elif osm_type == 'relation' and way_els:
            for way in way_els:
                if 'geometry' not in way:
                    continue
                seg_geo = [{'lat': p['lat'], 'lon': p['lon']} for p in way['geometry']]
                seg_name = way.get('tags', {}).get('name', f"Section {way['id']}")
                seg_dist = 0
                for i in range(len(seg_geo) - 1):
                    lat1, lon1 = radians(seg_geo[i]['lat']), radians(seg_geo[i]['lon'])
                    lat2, lon2 = radians(seg_geo[i+1]['lat']), radians(seg_geo[i+1]['lon'])
                    dlat = lat2 - lat1
                    dlon = lon2 - lon1
                    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
                    s = 6371 * 2 * atan2(sqrt(a), sqrt(1-a))
                    if s < 0.5:
                        seg_dist += s
                trail_segments.append({
                    'osm_id': way['id'],
                    'name': seg_name,
                    'surface': way.get('tags', {}).get('surface', ''),
                    'difficulty': way.get('tags', {}).get('sac_scale', ''),
                    'distance_km': round(seg_dist, 2),
                    'distance_mi': round(seg_dist * 0.621371, 2),
                    'geometry': seg_geo,
                })
        
        result['has_trailhead'] = has_trailhead
        result['access_trail'] = access_trail
        result['segments'] = trail_segments if len(trail_segments) > 1 else []
        
        # Enrich with USGS data if we have coordinates
        if lat and lon:
            try:
                usgs_trails = usgs_query_bbox(lat - 0.05, lon - 0.05, lat + 0.05, lon + 0.05)
                trail_name = result['name'].strip().lower()
                for ut in usgs_trails:
                    uname = ut['name'].strip().lower()
                    if uname and (uname in trail_name or trail_name in uname):
                        if ut.get('length_miles'):
                            result['length_miles'] = ut['length_miles']
                        if ut.get('activities'):
                            result['activities'] = ut['activities']
                        if ut.get('maintainer'):
                            result['maintainer'] = ut['maintainer']
                        if ut.get('usgs_trail_type'):
                            result['usgs_trail_type'] = ut['usgs_trail_type']
                        if ut.get('designation'):
                            result['designation'] = ut['designation']
                        result['usgs_enriched'] = True
                        break
            except Exception:
                pass
        
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
                'current': 'snow_depth,temperature_2m',
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
                current = d.get('current', {})
                elevation = d.get('elevation', 0)
                snow_depth_m = current.get('snow_depth', 0) or 0
                snow_depth_in = snow_depth_m * 39.37
                current_temp = current.get('temperature_2m', 10) or 10
                total_snow = sum(v for v in (daily.get('snowfall_sum') or []) if v)
                total_rain = sum(v for v in (daily.get('rain_sum') or []) if v)
                total_precip = sum(v for v in (daily.get('precipitation_sum') or []) if v)
                min_temps = [v for v in (daily.get('temperature_2m_min') or []) if v is not None]
                avg_min = sum(min_temps) / len(min_temps) if min_temps else 0
                elev_ft = elevation * 3.281 if elevation else 0
                
                reasons = []
                condition = 'clear'
                
                # Snow depth on ground is the strongest signal
                if snow_depth_in > 12:
                    condition = 'snowy'
                    reasons.append(f'{snow_depth_in:.0f}" snow depth on ground')
                    if current_temp < -2:
                        reasons.append(f'Currently {current_temp:.0f}°C — packed/icy snow likely')
                elif snow_depth_in > 3:
                    if current_temp < 0:
                        condition = 'icy'
                        reasons.append(f'{snow_depth_in:.0f}" snow + freezing ({current_temp:.0f}°C)')
                    else:
                        condition = 'snowy'
                        reasons.append(f'{snow_depth_in:.0f}" snow on ground')
                # Recent snowfall
                elif total_snow > 5:
                    condition = 'snowy'
                    reasons.append(f'{total_snow:.1f}cm snowfall in 7 days')
                elif total_snow > 0.5:
                    if avg_min < 0:
                        condition = 'icy'
                        reasons.append(f'{total_snow:.1f}cm snow + freezing temps')
                    else:
                        condition = 'snowy'
                        reasons.append(f'{total_snow:.1f}cm snow in 7 days')
                # Elevation-aware: high alpine with freezing temps likely has lingering snow/ice
                if condition == 'clear' and elev_ft > 8000 and avg_min < -3:
                    condition = 'icy'
                    reasons.append(f'High elevation ({elev_ft:.0f}ft) + freezing temps')
                elif condition == 'clear' and elev_ft > 6500 and avg_min < -5 and total_precip > 2:
                    condition = 'snowy'
                    reasons.append(f'Elevation {elev_ft:.0f}ft with sub-freezing temps + recent precip')
                # Rain/mud
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
                # Add elevation context
                if elev_ft > 5000:
                    reasons.append(f'Elevation: {elev_ft:.0f}ft')
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

@app.route('/api/quicklog', methods=['POST'])
def add_quicklog():
    data = request.json
    db = get_db()
    db.execute('''INSERT INTO quick_logs (log_type, facility_id, trail_name, lat, lon, category, detail, notes)
                  VALUES (?,?,?,?,?,?,?,?)''',
               (data.get('log_type'), data.get('facility_id'), data.get('trail_name'),
                data.get('lat'), data.get('lon'), data.get('category'),
                data.get('detail'), data.get('notes')))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/quicklogs/<path:facility_id>')
def get_quicklogs(facility_id):
    db = get_db()
    logs = db.execute('SELECT * FROM quick_logs WHERE facility_id=? ORDER BY created_at DESC LIMIT 50', (facility_id,)).fetchall()
    return jsonify([dict(r) for r in logs])

@app.route('/api/quicklogs/nearby')
def get_nearby_quicklogs():
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    radius = request.args.get('radius', 0.1, type=float)  # ~11km default
    if lat is None or lon is None:
        return jsonify({'error': 'lat/lon required'}), 400
    db = get_db()
    logs = db.execute('''SELECT * FROM quick_logs 
                         WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
                         ORDER BY created_at DESC LIMIT 100''',
                      (lat - radius, lat + radius, lon - radius, lon + radius)).fetchall()
    return jsonify([dict(r) for r in logs])

@app.route('/api/quicklogs/<int:log_id>/vote', methods=['POST'])
def vote_quicklog(log_id):
    data = request.json
    vote_type = data.get('vote_type', 'up')
    db = get_db()
    if vote_type == 'up':
        db.execute('UPDATE quick_logs SET upvotes=upvotes+1 WHERE id=?', (log_id,))
    else:
        db.execute('UPDATE quick_logs SET downvotes=downvotes+1 WHERE id=?', (log_id,))
    db.commit()
    return jsonify({'ok': True})

# Initialize DB on import (works with both gunicorn and direct run)
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8095, debug=False, threaded=True)

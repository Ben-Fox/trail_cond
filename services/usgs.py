import hashlib
import time
import logging
import requests
from math import radians, sin, cos, sqrt, atan2
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

USGS_TRAILS_URL = 'https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer/37/query'

usgs_session = requests.Session()
usgs_session.headers.update({'User-Agent': 'TrailCondish/1.0'})

_usgs_cache = {}
USGS_CACHE_TTL = 600  # 10 minutes
_usgs_executor = ThreadPoolExecutor(max_workers=2)


def _haversine_km_simple(lat1, lon1, lat2, lon2):
    """Quick haversine distance in km."""
    rlat1, rlon1, rlat2, rlon2 = radians(lat1), radians(lon1), radians(lat2), radians(lon2)
    dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
    a = sin(dlat/2)**2 + cos(rlat1)*cos(rlat2)*sin(dlon/2)**2
    return 6371 * 2 * atan2(sqrt(a), sqrt(1-a))


def _usgs_cache_key(params):
    return hashlib.md5(str(sorted(params.items())).encode()).hexdigest()


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


def usgs_query_bbox(south, west, north, east, trail_type='all', return_geometry=False, max_results=500):
    """Query USGS trails API for a bounding box."""
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
    params['where'] = "name IS NOT NULL AND name <> ''"

    cache_key = _usgs_cache_key(params)
    now = time.time()

    if cache_key in _usgs_cache:
        ts, data = _usgs_cache[cache_key]
        if now - ts < USGS_CACHE_TTL:
            return data

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


def merge_usgs_into_osm(osm_trails, usgs_trails):
    """Merge USGS trail data into OSM results."""
    if not usgs_trails:
        return osm_trails

    osm_by_name = {}
    for t in osm_trails:
        norm = t.get('name', '').strip().lower()
        if norm:
            if norm not in osm_by_name:
                osm_by_name[norm] = []
            osm_by_name[norm].append(t)

    usgs_by_name = {}
    for ut in usgs_trails:
        uname = ut['name'].strip().lower()
        if not uname:
            continue
        if uname not in usgs_by_name or (ut.get('length_miles') or 0) > (usgs_by_name[uname].get('length_miles') or 0):
            usgs_by_name[uname] = ut

    for uname, ut in usgs_by_name.items():
        if uname in osm_by_name:
            for osm_t in osm_by_name[uname]:
                _enrich_osm_with_usgs(osm_t, ut)
            continue

        for norm, osm_list in osm_by_name.items():
            if len(uname) > 4 and len(norm) > 4 and (uname in norm or norm in uname):
                for osm_t in osm_list:
                    _enrich_osm_with_usgs(osm_t, ut)
                break

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

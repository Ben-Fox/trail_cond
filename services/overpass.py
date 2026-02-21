import hashlib
import time
import logging
from math import radians, sin, cos, sqrt, atan2

from services import http_session

logger = logging.getLogger(__name__)

# Overpass cache
_overpass_cache = {}
CACHE_TTL = 600  # 10 minutes

OVERPASS_SERVERS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
]

_last_overpass_request = 0


def get_overpass_cache():
    """Expose cache for autocomplete to read."""
    return _overpass_cache


def _haversine_km(lat1, lon1, lat2, lon2):
    """Quick haversine distance in km."""
    rlat1, rlon1, rlat2, rlon2 = radians(lat1), radians(lon1), radians(lat2), radians(lon2)
    dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
    a = sin(dlat/2)**2 + cos(rlat1)*cos(rlat2)*sin(dlon/2)**2
    return 6371 * 2 * atan2(sqrt(a), sqrt(1-a))


def _cluster_ways(ways, max_merge_km=5.0):
    """Group ways into proximity clusters. Each cluster becomes one result."""
    clusters = []
    for w in ways:
        if w['lat'] is None or w['lon'] is None:
            clusters.append([w])
            continue
        merged = False
        for cluster in clusters:
            for cw in cluster:
                if cw['lat'] is not None and cw['lon'] is not None:
                    if _haversine_km(w['lat'], w['lon'], cw['lat'], cw['lon']) <= max_merge_km:
                        cluster.append(w)
                        merged = True
                        break
            if merged:
                break
        if not merged:
            clusters.append([w])
    return clusters


def overpass_query(query):
    global _last_overpass_request
    cache_key = hashlib.md5(query.encode()).hexdigest()
    now = time.time()

    if len(_overpass_cache) > 100:
        stale = [k for k, (ts, _) in _overpass_cache.items() if now - ts > CACHE_TTL]
        for k in stale:
            del _overpass_cache[k]

    if cache_key in _overpass_cache:
        ts, data = _overpass_cache[cache_key]
        if now - ts < CACHE_TTL:
            return data

    elapsed = now - _last_overpass_request
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)

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
    way_groups = {}
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

    rel_names = {r['name'].strip().lower() for r in relations}

    results = list(relations)
    for norm, ways in way_groups.items():
        if norm in rel_names:
            continue
        clusters = _cluster_ways(ways)
        for cluster in clusters:
            rep = cluster[0]
            tags = rep['tags']
            way_ids = [w['id'] for w in cluster]
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

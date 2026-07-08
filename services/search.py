import re
import time
from flask import Blueprint, request, jsonify

from services import http_session
from services.cache import cached_response, cache_response
from services.overpass import overpass_query, parse_osm_trails, get_overpass_cache, CACHE_TTL
from services.usgs import merge_usgs_into_osm, fetch_usgs_for_bbox

search_bp = Blueprint('search', __name__)

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'

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


def _sanitize_overpass(s):
    """Sanitize user input for safe use in Overpass QL regex queries."""
    # Remove characters that could break Overpass QL syntax
    return re.sub(r'["\\\];(){}\n\r]', '', s)[:100]


def geocode(query):
    r = http_session.get(NOMINATIM_URL, params={
        'q': query, 'format': 'json', 'limit': 10, 'countrycodes': 'us'
    }, timeout=10)
    results = r.json()
    if not results:
        return None

    def score(res):
        # Higher is better. Prominence (Nominatim's importance, roughly
        # population/notability) is the main signal, so an ambiguous name like
        # "Denver" resolves to the big well-known one. A small nudge toward
        # populated places and admin areas keeps a same-name city ahead of a
        # minor creek/park that happens to share the name.
        try:
            imp = float(res.get('importance', 0) or 0)
        except (TypeError, ValueError):
            imp = 0.0
        cls = res.get('class', '')
        typ = res.get('type', '')
        bonus = 0.0
        if cls == 'place' and typ in ('city', 'town', 'village', 'hamlet'):
            bonus = 0.15
        elif cls == 'boundary' and typ == 'administrative':
            bonus = 0.10
        return imp + bonus

    best = max(results, key=score)
    bbox = best.get('boundingbox', [])
    return {
        'lat': float(best['lat']),
        'lon': float(best['lon']),
        'bbox': [float(b) for b in bbox] if len(bbox) == 4 else None
    }


AUTOCOMPLETE_TTL = 300


@search_bp.route('/api/autocomplete')
def api_autocomplete():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])

    ac_key = f"ac:{q.lower()}"
    now = time.time()
    cached = cached_response(ac_key, ttl=AUTOCOMPLETE_TTL)
    if cached is not None:
        return jsonify(cached)

    results = []

    try:
        r = http_session.get('https://photon.komoot.io/api/', params={
            'q': q, 'limit': 15, 'lang': 'en',
            'bbox': '-125,24,-66,50',
        }, timeout=5)
        features = r.json().get('features', [])

        def place_score(f):
            props = f.get('properties', {})
            osm_value = props.get('osm_value', '')
            osm_key = props.get('osm_key', '')
            score = 0
            if osm_key == 'place' and osm_value in ('city', 'town', 'village', 'hamlet', 'suburb', 'borough'):
                score += 5
            elif osm_key == 'natural' or osm_value in ('peak', 'mountain', 'lake', 'river', 'valley', 'water'):
                score += 3
            elif osm_key == 'leisure' or 'park' in osm_value or 'forest' in osm_value:
                score += 2
            elif osm_key == 'highway' and osm_value in ('path', 'footway', 'track'):
                score += 3
            elif osm_key == 'boundary' and 'national' in props.get('name', '').lower():
                score += 2
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

    _overpass_cache = get_overpass_cache()
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

    cache_response(ac_key, results, ttl=AUTOCOMPLETE_TTL)
    return jsonify(results)


@search_bp.route('/api/search')
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
        if q and not bbox:
            # Text query takes priority — handle it first
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

                usgs_future = fetch_usgs_for_bbox(
                    south, west, north, east, trail_type)

            elif geo:
                around_query = f'''[out:json][timeout:25];
(
  way{tp['ways']}["name"](around:15000,{geo['lat']},{geo['lon']});
  relation{tp['rels']}["name"](around:15000,{geo['lat']},{geo['lon']});
);
out center tags 100;'''
                results = parse_osm_trails(overpass_query(around_query))

            if not results:
                safe_q = _sanitize_overpass(q)
                name_query = f'''[out:json][timeout:25];
(
  way["highway"~"path|footway|track|bridleway|cycleway"]["name"~"{safe_q}",i];
  relation["route"="hiking"]["name"~"{safe_q}",i];
);
out center tags 20;'''
                name_results = parse_osm_trails(overpass_query(name_query))
                results = name_results

            if usgs_future:
                try:
                    usgs_trails = usgs_future.result(timeout=3)
                    results = merge_usgs_into_osm(results, usgs_trails)
                except Exception:
                    pass

            response = {'trails': results[:100]}
            if geo:
                response['center'] = {'lat': geo['lat'], 'lon': geo['lon']}
                if geo.get('bbox'):
                    response['bbox'] = geo['bbox']
            return jsonify(response)

        elif bbox:
            parts = bbox.split(',')
            if len(parts) != 4:
                return jsonify([])
            s, w, n, e = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            usgs_future = fetch_usgs_for_bbox(s, w, n, e, trail_type)
            query = f'''[out:json][timeout:25];
(
  way{tp['ways']}["name"]({s},{w},{n},{e});
  relation{tp['rels']}["name"]({s},{w},{n},{e});
);
out center tags 100;'''
            results = parse_osm_trails(overpass_query(query))
            try:
                usgs_trails = usgs_future.result(timeout=3)
                results = merge_usgs_into_osm(results, usgs_trails)
            except Exception:
                pass
            return jsonify(results)
        elif lat and lon:
            latf, lonf = float(lat), float(lon)
            usgs_future = fetch_usgs_for_bbox(
                latf - 0.1, lonf - 0.1, latf + 0.1, lonf + 0.1, trail_type)
            query = f'''[out:json][timeout:25];
(
  way{tp['ways']}["name"](around:8000,{latf},{lonf});
  relation{tp['rels']}["name"](around:8000,{latf},{lonf});
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
        else:
            return jsonify([])

        results = parse_osm_trails(overpass_query(query))
        if usgs_future:
            try:
                usgs_trails = usgs_future.result(timeout=3)
                results = merge_usgs_into_osm(results, usgs_trails)
            except Exception:
                pass
        return jsonify(results[:100])
    except Exception:
        return jsonify({'error': 'Search temporarily unavailable'}), 500

from math import radians, sin, cos, sqrt, atan2
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, request, jsonify

from services.cache import cached_response, cache_response
from services.overpass import overpass_query, CACHE_TTL
from services.usgs import usgs_query_bbox
from services import http_session

_executor = ThreadPoolExecutor(max_workers=4)

trail_bp = Blueprint('trail', __name__)


def _pt_dist_m(p1, p2):
    """Distance in meters between two {lat, lon} dicts."""
    r = 6371000
    dlat = radians(p2['lat'] - p1['lat'])
    dlon = radians(p2['lon'] - p1['lon'])
    a = sin(dlat/2)**2 + cos(radians(p1['lat']))*cos(radians(p2['lat']))*sin(dlon/2)**2
    return r * 2 * atan2(sqrt(a), sqrt(1-a))


def _stitch_ways(way_geometries):
    """Stitch multiple way geometries into a continuous line.
    
    Each way geometry is a list of {lat, lon} points. Ways connect when 
    one's endpoint is near another's endpoint (shared OSM node = exact match,
    but we use 15m tolerance for floating point / split nodes).
    
    Algorithm:
    1. Start with the first way as the "chain"
    2. Repeatedly find the unplaced way whose endpoint is closest to 
       either end of the chain
    3. Append/prepend (reversing if needed) to grow the chain
    4. If no way connects within tolerance, start a new chain segment
    """
    if not way_geometries:
        return []
    if len(way_geometries) == 1:
        return way_geometries[0]

    CONNECT_THRESHOLD_M = 15  # meters — shared nodes should be <1m, allow slack

    remaining = [list(g) for g in way_geometries]
    # Start chain with the first way
    chain = remaining.pop(0)

    max_iterations = len(remaining) * 2  # safety valve
    iterations = 0
    while remaining and iterations < max_iterations:
        iterations += 1
        chain_start = chain[0]
        chain_end = chain[-1]
        
        best_idx = None
        best_dist = float('inf')
        best_mode = None  # 'append', 'append_rev', 'prepend', 'prepend_rev'

        for i, seg in enumerate(remaining):
            if len(seg) < 2:
                continue
            seg_start = seg[0]
            seg_end = seg[-1]

            # Can this segment attach to the END of the chain?
            d_end_start = _pt_dist_m(chain_end, seg_start)  # chain_end → seg_start (append as-is)
            d_end_end = _pt_dist_m(chain_end, seg_end)      # chain_end → seg_end (append reversed)
            # Can this segment attach to the START of the chain?
            d_start_end = _pt_dist_m(chain_start, seg_end)  # seg_end → chain_start (prepend as-is)
            d_start_start = _pt_dist_m(chain_start, seg_start)  # seg_start → chain_start (prepend reversed)

            candidates = [
                (d_end_start, 'append', i),
                (d_end_end, 'append_rev', i),
                (d_start_end, 'prepend', i),
                (d_start_start, 'prepend_rev', i),
            ]
            for d, mode, idx in candidates:
                if d < best_dist:
                    best_dist = d
                    best_idx = idx
                    best_mode = mode

        if best_idx is None or best_dist > CONNECT_THRESHOLD_M:
            # No connecting segment found within threshold — 
            # just append remaining segments with a gap
            for seg in remaining:
                chain.extend(seg)
            break

        seg = remaining.pop(best_idx)
        if best_mode == 'append':
            chain.extend(seg[1:])  # skip first point (duplicate of chain end)
        elif best_mode == 'append_rev':
            seg.reverse()
            chain.extend(seg[1:])
        elif best_mode == 'prepend':
            chain = seg + chain[1:]  # skip first point of old chain (dup of seg end)
        elif best_mode == 'prepend_rev':
            seg.reverse()
            chain = seg + chain[1:]

    return chain


def _reverse_geocode(lat, lon):
    """Get nearest city/town + state via Nominatim, and check for public land via Overpass."""
    location = {}

    # Nominatim reverse geocode
    try:
        r = http_session.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={'lat': lat, 'lon': lon, 'format': 'json', 'zoom': 14, 'addressdetails': 1},
            headers={'User-Agent': 'TrailCondish/1.0'},
            timeout=5
        )
        if r.ok:
            addr = r.json().get('address', {})
            city = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('hamlet') or ''
            state = addr.get('state', '')
            county = addr.get('county', '')
            if city:
                location['city'] = city
            if state:
                location['state'] = state
            if county:
                location['county'] = county
    except Exception:
        pass

    # Overpass: check if point is inside a national forest, national park, or BLM land
    try:
        land_query = f'''[out:json][timeout:10];
(
  relation(around:100,{lat},{lon})["boundary"="national_park"];
  relation(around:100,{lat},{lon})["boundary"="protected_area"]["protect_class"~"^(2|3|4|5|6)$"];
  relation(around:100,{lat},{lon})["leisure"="nature_reserve"];
  way(around:100,{lat},{lon})["boundary"="national_park"];
  way(around:100,{lat},{lon})["boundary"="protected_area"]["protect_class"~"^(2|3|4|5|6)$"];
);
out tags 3;'''
        land_data = overpass_query(land_query)
        land_els = land_data.get('elements', [])

        for le in land_els:
            t = le.get('tags', {})
            name = t.get('name', '')
            if not name:
                continue
            name_lower = name.lower()
            if 'national forest' in name_lower or 'national grassland' in name_lower:
                location['public_land'] = name
                location['land_type'] = 'National Forest'
                break
            elif 'national park' in name_lower:
                location['public_land'] = name
                location['land_type'] = 'National Park'
                break
            elif 'blm' in name_lower or 'bureau of land management' in name_lower:
                location['public_land'] = name
                location['land_type'] = 'BLM Land'
                break
            elif 'wilderness' in name_lower:
                location['public_land'] = name
                location['land_type'] = 'Wilderness Area'
                break
            elif 'state park' in name_lower or 'state forest' in name_lower:
                location['public_land'] = name
                location['land_type'] = 'State Land'
                break
            elif 'national monument' in name_lower:
                location['public_land'] = name
                location['land_type'] = 'National Monument'
                break
            elif name:
                # Generic protected area
                location['public_land'] = name
                operator = t.get('operator', '')
                if 'blm' in operator.lower() or 'bureau of land' in operator.lower():
                    location['land_type'] = 'BLM Land'
                elif 'forest service' in operator.lower() or 'usfs' in operator.lower():
                    location['land_type'] = 'National Forest'
                else:
                    location['land_type'] = 'Protected Area'
                break
    except Exception:
        pass

    return location


@trail_bp.route('/api/trail/<osm_type>/<int:osm_id>')
def api_trail(osm_type, osm_id):
    if osm_type not in ('way', 'relation'):
        return jsonify({'error': 'Invalid type'}), 400

    extra_way_ids = request.args.get('way_ids', '')
    cache_key = f"trail:{osm_type}:{osm_id}:{extra_way_ids}"
    cached = cached_response(cache_key, ttl=CACHE_TTL)
    if cached:
        return jsonify(cached)

    try:
        if osm_type == 'way' and extra_way_ids:
            all_ids = [str(osm_id)] + [i.strip() for i in extra_way_ids.split(',') if i.strip()]
            all_ids = list(dict.fromkeys(all_ids))
            id_union = ''.join(f'way({wid});' for wid in all_ids)
            query = f'''[out:json][timeout:25];
({id_union});\nout geom tags;'''
        elif osm_type == 'relation':
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

        if osm_type == 'relation':
            rel_els = [e for e in elements if e.get('type') == 'relation']
            way_els = [e for e in elements if e.get('type') == 'way' and 'geometry' in e]
            el = rel_els[0] if rel_els else elements[0]
            tags = el.get('tags', {})
        else:
            el = elements[0]
            tags = el.get('tags', {})
            way_els = []

        geometry = []
        if osm_type == 'way':
            way_geos = []
            for seg in elements:
                if 'geometry' in seg:
                    way_geos.append([{'lat': p['lat'], 'lon': p['lon']} for p in seg['geometry']])
            if len(way_geos) > 1:
                geometry = _stitch_ways(way_geos)
            elif way_geos:
                geometry = way_geos[0]
        elif osm_type == 'relation':
            way_geos = []
            for way in way_els:
                if 'geometry' in way:
                    way_geos.append([{'lat': p['lat'], 'lon': p['lon']} for p in way['geometry']])
            if len(way_geos) > 1:
                geometry = _stitch_ways(way_geos)
            elif way_geos:
                geometry = way_geos[0]

        lat = lon = None
        if geometry:
            lat = geometry[0]['lat']
            lon = geometry[0]['lon']
        elif 'center' in el:
            lat = el['center']['lat']
            lon = el['center']['lon']

        if len(geometry) > 1:
            deduped = [geometry[0]]
            for pt in geometry[1:]:
                if pt['lat'] != deduped[-1]['lat'] or pt['lon'] != deduped[-1]['lon']:
                    deduped.append(pt)
            geometry = deduped

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
                if seg < 0.5:
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

        trail_segments = []

        # Build segments from multi-way trails
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

        result['segments'] = trail_segments if len(trail_segments) > 1 else []

        # === Run trailhead detection, reverse geocode, and USGS enrichment in PARALLEL ===
        def _trailhead_task():
            """Detect trailhead access or connecting trail."""
            _has_trailhead = False
            _access_trail = None
            if not geometry or len(geometry) <= 1:
                return _has_trailhead, _access_trail
            start_pt = geometry[0]
            end_pt = geometry[-1]
            try:
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
                _has_trailhead = len(th_data.get('elements', [])) > 0
                if not _has_trailhead:
                    conn_query = f'''[out:json][timeout:10];
(
  way(around:30,{start_pt['lat']},{start_pt['lon']})["highway"~"^(path|footway|track|bridleway|cycleway)$"]["name"];
  way(around:30,{end_pt['lat']},{end_pt['lon']})["highway"~"^(path|footway|track|bridleway|cycleway)$"]["name"];
);
out center tags;'''
                    conn_data = overpass_query(conn_query)
                    trail_name_lower = result['name'].lower().strip()
                    for ce in conn_data.get('elements', []):
                        ce_name = ce.get('tags', {}).get('name', '').strip()
                        if ce_name and ce_name.lower() != trail_name_lower:
                            _access_trail = {
                                'name': ce_name, 'osm_type': 'way', 'osm_id': ce['id'],
                                'lat': ce.get('center', {}).get('lat'),
                                'lon': ce.get('center', {}).get('lon'),
                            }
                            break
            except Exception:
                pass
            return _has_trailhead, _access_trail

        def _usgs_task():
            """Enrich trail with USGS data."""
            enrichment = {}
            if not lat or not lon:
                return enrichment
            try:
                usgs_trails = usgs_query_bbox(lat - 0.05, lon - 0.05, lat + 0.05, lon + 0.05)
                trail_name = result['name'].strip().lower()
                for ut in usgs_trails:
                    uname = ut['name'].strip().lower()
                    if uname and (uname in trail_name or trail_name in uname):
                        for k in ('length_miles', 'activities', 'maintainer', 'usgs_trail_type', 'designation'):
                            if ut.get(k):
                                enrichment[k] = ut[k]
                        enrichment['usgs_enriched'] = True
                        break
            except Exception:
                pass
            return enrichment

        def _location_task():
            """Reverse geocode for location context."""
            if not lat or not lon:
                return {}
            try:
                return _reverse_geocode(lat, lon)
            except Exception:
                return {}

        def _crossings_task():
            """Detect bridges and fords along the trail, deduplicated by proximity."""
            crossings = []
            if not geometry or len(geometry) < 2:
                return crossings
            try:
                # Sample up to 10 points along the trail for the query
                step = max(1, len(geometry) // 10)
                sample_pts = [geometry[i] for i in range(0, len(geometry), step)]
                if geometry[-1] not in sample_pts:
                    sample_pts.append(geometry[-1])

                around_sets = []
                for pt in sample_pts[:10]:
                    around_sets.append(f'node(around:30,{pt["lat"]},{pt["lon"]})["ford"];')
                    around_sets.append(f'way(around:30,{pt["lat"]},{pt["lon"]})["ford"];')
                    around_sets.append(f'way(around:30,{pt["lat"]},{pt["lon"]})["bridge"]["bridge"!="no"];')
                    around_sets.append(f'node(around:30,{pt["lat"]},{pt["lon"]})["bridge"]["bridge"!="no"];')

                q = f'''[out:json][timeout:10];
({chr(10).join(around_sets)});
out center tags;'''
                data = overpass_query(q)
                seen_ids = set()
                raw = []
                for el in data.get('elements', []):
                    tags = el.get('tags', {})
                    el_id = el.get('id')
                    if el_id in seen_ids:
                        continue
                    seen_ids.add(el_id)

                    c_lat = el.get('lat') or (el.get('center', {}).get('lat'))
                    c_lon = el.get('lon') or (el.get('center', {}).get('lon'))
                    if not c_lat or not c_lon:
                        continue

                    if tags.get('ford') and tags['ford'] != 'no':
                        raw.append({
                            'type': 'ford',
                            'name': tags.get('name', ''),
                            'lat': c_lat, 'lon': c_lon,
                            'depth': tags.get('depth', ''),
                        })
                    elif tags.get('bridge') and tags['bridge'] != 'no':
                        raw.append({
                            'type': 'bridge',
                            'name': tags.get('name', ''),
                            'lat': c_lat, 'lon': c_lon,
                            'material': tags.get('material', tags.get('bridge:structure', '')),
                        })

                # Deduplicate by proximity: crossings within 80m of each
                # other are the same physical crossing
                def _dist_m(lat1, lon1, lat2, lon2):
                    r = 6371000
                    dlat = radians(lat2 - lat1)
                    dlon = radians(lon2 - lon1)
                    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
                    return r * 2 * atan2(sqrt(a), sqrt(1-a))

                for c in raw:
                    is_dup = False
                    for existing in crossings:
                        if existing['type'] != c['type']:
                            continue
                        # Only dedup if literally the same spot (<30m)
                        if _dist_m(existing['lat'], existing['lon'], c['lat'], c['lon']) < 30:
                            is_dup = True
                            break
                    if not is_dup:
                        crossings.append(c)

            except Exception:
                pass
            return crossings

        # Fire all four in parallel
        futures = {
            _executor.submit(_trailhead_task): 'trailhead',
            _executor.submit(_usgs_task): 'usgs',
            _executor.submit(_location_task): 'location',
            _executor.submit(_crossings_task): 'crossings',
        }

        has_trailhead = False
        access_trail = None
        for future in as_completed(futures, timeout=15):
            key = futures[future]
            try:
                if key == 'trailhead':
                    has_trailhead, access_trail = future.result()
                elif key == 'usgs':
                    result.update(future.result())
                elif key == 'location':
                    result['location'] = future.result()
                elif key == 'crossings':
                    result['water_crossings'] = future.result()
            except Exception:
                if key == 'location':
                    result['location'] = {}
                elif key == 'crossings':
                    result['water_crossings'] = []

        result['has_trailhead'] = has_trailhead
        result['access_trail'] = access_trail

        cache_response(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

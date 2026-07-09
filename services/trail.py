import logging
from math import radians, sin, cos, sqrt, atan2
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from flask import Blueprint, request, jsonify

from services.cache import cached_response, cache_response
from services.overpass import overpass_query, CACHE_TTL
from services.usgs import usgs_query_bbox
from services import http_session

_executor = ThreadPoolExecutor(max_workers=4)
logger = logging.getLogger(__name__)

trail_bp = Blueprint('trail', __name__)


def _pt_dist_m(p1, p2):
    """Distance in meters between two {lat, lon} dicts."""
    r = 6371000
    dlat = radians(p2['lat'] - p1['lat'])
    dlon = radians(p2['lon'] - p1['lon'])
    a = sin(dlat/2)**2 + cos(radians(p1['lat']))*cos(radians(p2['lat']))*sin(dlon/2)**2
    return r * 2 * atan2(sqrt(a), sqrt(1-a))


def _stitch_ways_multi(way_geometries, connect_threshold_m=60):
    """Stitch way geometries into continuous chains (a MultiLineString).

    Greedy: grow the current chain by whichever unplaced way's endpoint is
    nearest either chain end (reversing as needed). When nothing connects
    within the threshold, CLOSE the chain and start a new one — never
    concatenate disconnected pieces (the old behavior drew long straight
    'rubber band' lines across the map).

    60 m tolerance: shared OSM nodes match at <1 m, but named trails are
    routinely split by road crossings and short gaps.

    Returns a list of chains, longest first; each chain is a point list.
    """
    ways = [list(g) for g in way_geometries if len(g) >= 2]
    if not ways:
        return []
    if len(ways) == 1:
        return [ways[0]]

    chains = []
    remaining = ways
    chain = remaining.pop(0)

    while True:
        best_idx = None
        best_dist = float('inf')
        best_mode = None
        chain_start, chain_end = chain[0], chain[-1]

        for i, seg in enumerate(remaining):
            for d, mode in ((_pt_dist_m(chain_end, seg[0]), 'append'),
                            (_pt_dist_m(chain_end, seg[-1]), 'append_rev'),
                            (_pt_dist_m(chain_start, seg[-1]), 'prepend'),
                            (_pt_dist_m(chain_start, seg[0]), 'prepend_rev')):
                if d < best_dist:
                    best_dist, best_idx, best_mode = d, i, mode

        if best_idx is not None and best_dist <= connect_threshold_m:
            seg = remaining.pop(best_idx)
            if best_mode == 'append':
                chain.extend(seg[1:] if best_dist < 1 else seg)
            elif best_mode == 'append_rev':
                seg.reverse()
                chain.extend(seg[1:] if best_dist < 1 else seg)
            elif best_mode == 'prepend':
                chain = seg + (chain[1:] if best_dist < 1 else chain)
            else:
                seg.reverse()
                chain = seg + (chain[1:] if best_dist < 1 else chain)
            if remaining:
                continue

        chains.append(chain)
        if not remaining:
            break
        chain = remaining.pop(0)

    chains.sort(key=len, reverse=True)
    return chains


def _stitch_ways(way_geometries):
    """Back-compat single-chain view: the LONGEST stitched chain."""
    chains = _stitch_ways_multi(way_geometries)
    return chains[0] if chains else []


def _chain_length_km(chain):
    total = 0.0
    for i in range(len(chain) - 1):
        total += _pt_dist_m(chain[i], chain[i + 1]) / 1000.0
    return total


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

        if osm_type == 'way':
            src_els = elements
        else:
            src_els = way_els
        way_geos = []
        for seg in src_els:
            if 'geometry' in seg:
                way_geos.append([{'lat': p['lat'], 'lon': p['lon']} for p in seg['geometry']])
        chains = _stitch_ways_multi(way_geos)
        # dedupe consecutive duplicate points per chain
        for ci, ch in enumerate(chains):
            deduped = [ch[0]]
            for pt in ch[1:]:
                if pt['lat'] != deduped[-1]['lat'] or pt['lon'] != deduped[-1]['lon']:
                    deduped.append(pt)
            chains[ci] = deduped
        geometry = chains[0] if chains else []   # longest chain (pin/elevation/back-compat)

        lat = lon = None
        if geometry:
            lat = geometry[0]['lat']
            lon = geometry[0]['lon']
        elif 'center' in el:
            lat = el['center']['lat']
            lon = el['center']['lon']

        # distance: sum over ALL stitched chains (no rubber-band jumps to hide now)
        distance_km = None
        if chains and any(len(c) > 1 for c in chains):
            distance_km = round(sum(_chain_length_km(c) for c in chains), 1)

        result = {
            'osm_type': osm_type,
            'osm_id': osm_id,
            'name': tags.get('name', f'{osm_type}/{osm_id}'),
            'desc': tags.get('description', tags.get('note', '')),
            'lat': lat,
            'lon': lon,
            'geometry': geometry,
            'geometry_segments': chains,
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
        result['location'] = {}
        result['water_crossings'] = []

        try:
            for future in as_completed(futures, timeout=20):
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
                    pass  # Individual task failed, skip it
        except TimeoutError:
            pass  # Some tasks didn't finish in time, use what we got
        finally:
            # Cancel any still-running futures
            for future in futures:
                future.cancel()

        result['has_trailhead'] = has_trailhead
        result['access_trail'] = access_trail

        cache_response(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
# Route Builder — compose loops / out-and-backs near a target distance
# ═══════════════════════════════════════════════════════════════════════════

def _snap_key(pt, grid=0.00025):
    """Node key: ~25m snap grid so nearby endpoints share a graph node."""
    return (round(pt['lat'] / grid), round(pt['lon'] / grid))


def _build_route_graph(edges_pts):
    """edges_pts: list of dicts(name, pts). Splits edges at T-junctions
    (an endpoint of one way touching the middle of another) and returns
    (edges, adjacency) where edges[i] = dict(name, pts, km, a, b)."""
    # collect all endpoints
    endpoints = []
    for e in edges_pts:
        endpoints.append(e['pts'][0])
        endpoints.append(e['pts'][-1])

    # split edges where a FOREIGN endpoint lands on an interior vertex (~30m).
    # A way's own endpoints are excluded: cutting next to them creates tiny
    # stubs whose removal would orphan the trailhead node from the graph.
    split_edges = []
    for e in edges_pts:
        pts = e['pts']
        own = (pts[0], pts[-1])
        cut_idx = set()
        for k in range(1, len(pts) - 1):
            for ep in endpoints:
                if ep is own[0] or ep is own[1]:
                    continue
                dlat = (pts[k]['lat'] - ep['lat']) * 111000
                dlon = (pts[k]['lon'] - ep['lon']) * 85000
                if dlat * dlat + dlon * dlon < 30 * 30:
                    cut_idx.add(k)
                    break
        idxs = [0] + sorted(cut_idx) + [len(pts) - 1]
        for a, b in zip(idxs, idxs[1:]):
            if b > a:
                seg = pts[a:b + 1]
                if len(seg) >= 2:
                    split_edges.append(dict(name=e['name'], pts=seg))

    edges = []
    adj = {}
    for e in split_edges:
        km = _chain_length_km(e['pts'])
        a, b = _snap_key(e['pts'][0]), _snap_key(e['pts'][-1])
        if km < 0.005 and a == b:
            continue   # degenerate speck; short connector edges are kept
        i = len(edges)
        edges.append(dict(name=e['name'], pts=e['pts'], km=km, a=a, b=b))
        adj.setdefault(a, []).append((i, b))
        adj.setdefault(b, []).append((i, a))
    return edges, adj


def _search_route(edges, adj, start, target_km, mode, max_expansions=200000):
    """DFS over the edge graph.

    outback: simple path from start; total = 2 x path length.
    loop: LOLLIPOP-aware cycle search — a stem from the trailhead into the
    network, a cycle, and the stem back (stem may be empty for a pure loop).
    Returns (stem_edges, cycle_edges, total_km) for loop,
            (path_edges, None, total_km) for outback; path None if nothing found.
    """
    best = {'score': float('inf'), 'path': None, 'cycle': None, 'km': 0}
    expansions = [0]
    limit = target_km * (0.75 if mode == 'outback' else 0.9)
    MAX_DEPTH = 200

    def dfs(node, km, path, used, nodes, node_pos, km_at):
        if expansions[0] > max_expansions or len(path) > MAX_DEPTH:
            return
        expansions[0] += 1
        if mode == 'outback' and path:
            score = abs(km * 2 - target_km)
            if score < best['score']:
                best.update(score=score, path=list(path), cycle=None, km=km * 2)
        for (ei, other) in adj.get(node, []):
            if ei in used:
                continue
            e = edges[ei]
            nk = km + e['km']
            if mode == 'loop' and other in node_pos:
                p = node_pos[other]
                stem_km = km_at[p]
                cyc_km = nk - stem_km
                total = stem_km * 2 + cyc_km
                if cyc_km >= max(0.3, target_km * 0.15):
                    score = abs(total - target_km)
                    if score < best['score']:
                        best.update(score=score, path=path[:p], cycle=path[p:] + [ei], km=total)
                continue
            if mode == 'outback' and other in node_pos:
                # keep out-and-back a node-simple path (no looping back on itself);
                # this also keeps node_pos balanced so its set/del never corrupts.
                continue
            if nk > limit and mode == 'outback':
                continue
            if mode == 'loop' and nk > target_km * 1.1:
                continue
            used.add(ei)
            path.append(ei)
            nodes.append(other)
            node_pos[other] = len(nodes) - 1
            km_at.append(nk)
            dfs(other, nk, path, used, nodes, node_pos, km_at)
            km_at.pop()
            del node_pos[other]
            nodes.pop()
            path.pop()
            used.discard(ei)

    dfs(start, 0.0, [], set(), [start], {start: 0}, [0.0])
    return best['path'], best['cycle'], best['km']


def _walk_edges(edges, path, start_node):
    """Ordered, oriented points + names + end node for an edge sequence."""
    geom = []
    names = []
    node = start_node
    for ei in path:
        e = edges[ei]
        pts = e['pts'] if e['a'] == node else list(reversed(e['pts']))
        node = e['b'] if e['a'] == node else e['a']
        geom.extend(pts if not geom else pts[1:])
        if e['name'] and (not names or names[-1] != e['name']):
            names.append(e['name'])
    return geom, names, node


def _route_geometry(edges, stem, cycle, start):
    """Full route geometry: outback = path out (client doubles conceptually);
    loop = stem + cycle + stem reversed (lollipop)."""
    if cycle is None:
        geom, names, _ = _walk_edges(edges, stem, start)
        return geom, names
    stem_geom, stem_names, mid = _walk_edges(edges, stem, start)
    cyc_geom, cyc_names, _ = _walk_edges(edges, cycle, mid)
    geom = stem_geom + (cyc_geom if not stem_geom else cyc_geom[1:]) + stem_geom[::-1][1:]
    names = stem_names + [n for n in cyc_names if n not in stem_names]
    return geom, names


@trail_bp.route('/api/route/suggest')
def api_route_suggest():
    osm_type = request.args.get('osm_type', 'way')
    try:
        osm_id = int(request.args.get('osm_id', 0))
        target_km = float(request.args.get('target_km', 8.0))
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid parameters'}), 400
    mode = request.args.get('mode', 'loop')
    if mode not in ('loop', 'outback') or not (0.5 <= target_km <= 80) or osm_type not in ('way', 'relation'):
        return jsonify({'error': 'invalid parameters'}), 400
    way_ids = request.args.get('way_ids', '')

    cache_key = f"route:{osm_type}:{osm_id}:{way_ids}:{mode}:{target_km:.1f}"
    cached = cached_response(cache_key, ttl=1800)
    if cached:
        return jsonify(cached)

    try:
        # anchor geometry
        if osm_type == 'relation':
            q = f'[out:json][timeout:25];relation({osm_id});>;out geom;'
        else:
            ids = [str(osm_id)] + [i.strip() for i in way_ids.split(',') if i.strip()]
            union = ''.join(f'way({w});' for w in dict.fromkeys(ids))
            q = f'[out:json][timeout:25];({union});out geom;'
        anchor = overpass_query(q)
        anchor_pts = []
        for el in anchor.get('elements', []):
            if el.get('type') == 'way' and 'geometry' in el:
                anchor_pts.append(dict(
                    name='(this trail)',
                    pts=[{'lat': p['lat'], 'lon': p['lon']} for p in el['geometry']]))
        if not anchor_pts:
            return jsonify({'error': 'Trail geometry not found'}), 404

        lats = [p['lat'] for e in anchor_pts for p in e['pts']]
        lons = [p['lon'] for e in anchor_pts for p in e['pts']]
        # quantized buffer: nearby target distances share one cached network query
        raw_buf = max(0.01, min(0.09, target_km / 111.0 * 0.7))
        buf = min([0.02, 0.045, 0.09], key=lambda b: abs(b - raw_buf) if b >= raw_buf else 1e9)
        bbox = (min(lats) - buf, min(lons) - buf, max(lats) + buf, max(lons) + buf)

        # nearby named path network
        def _net_query(b):
            q2 = (f'[out:json][timeout:40];'
                  f'way["highway"~"^(path|footway|track|cycleway|bridleway)$"]'
                  f'({b[0]},{b[1]},{b[2]},{b[3]});out geom 350;')
            return overpass_query(q2, read_timeout=50)
        try:
            net = _net_query(bbox)
        except Exception:
            # dense areas can make the big query too expensive; retry small
            small = (min(lats) - 0.02, min(lons) - 0.02, max(lats) + 0.02, max(lons) + 0.02)
            net = _net_query(small)
        edges_pts = list(anchor_pts)
        anchor_ids = {osm_id} | {int(i) for i in way_ids.split(',') if i.strip().isdigit()}
        for el in net.get('elements', []):
            if el.get('type') == 'way' and 'geometry' in el and el.get('id') not in anchor_ids:
                edges_pts.append(dict(
                    name=(el.get('tags', {}) or {}).get('name', ''),
                    pts=[{'lat': p['lat'], 'lon': p['lon']} for p in el['geometry']]))

        edges, adj = _build_route_graph(edges_pts)
        start = _snap_key(anchor_pts[0]['pts'][0])
        if start not in adj:
            # trailhead stub may have been pruned; snap to nearest graph node
            sp = anchor_pts[0]['pts'][0]
            best_node, best_d = None, 1e18
            for node in adj:
                dlat = (node[0] * 0.00025 - sp['lat']) * 111000
                dlon = (node[1] * 0.00025 - sp['lon']) * 85000
                d = dlat * dlat + dlon * dlon
                if d < best_d:
                    best_d, best_node = d, node
            if best_node is None or best_d > 150 * 150:
                return jsonify({'error': 'Trailhead not connected to network'}), 404
            start = best_node

        stem, cycle, km = _search_route(edges, adj, start, target_km, mode)
        if stem is None and cycle is None:
            return jsonify({'error': 'No suitable route found — try a different distance'}), 404

        geom, names = _route_geometry(edges, stem or [], cycle, start)
        total = km
        n_seg = len(stem or []) + len(cycle or [])
        result = {
            'mode': mode,
            'target_km': target_km,
            'total_km': round(total, 2),
            'total_mi': round(total * 0.621371, 2),
            'names': [n for n in names if n][:12],
            'n_segments': n_seg,
            'geometry': geom,
        }
        cache_response(cache_key, result, ttl=1800)
        return jsonify(result)
    except Exception as e:
        logger.warning(f'route suggest failed: {e}')
        return jsonify({'error': 'Trail network lookup timed out — try again in a minute'}), 503


@trail_bp.route('/api/route/discover')
def api_route_discover():
    """Suggest loop / out-and-back route link-ups in the map bbox whose TRUE distance
    lands within a length range. Out-and-backs count double (round trip to the car)."""
    try:
        s, w, n, e = [float(x) for x in request.args.get('bbox', '').split(',')]
    except (ValueError, TypeError):
        return jsonify({'error': 'bbox=s,w,n,e required'}), 400

    def _f(name, dflt):
        try:
            return float(request.args.get(name, ''))
        except (TypeError, ValueError):
            return dflt

    min_mi = max(0.0, _f('min_mi', 0.0))
    max_mi = _f('max_mi', 50.0)
    if max_mi <= 0 or min_mi >= max_mi:
        return jsonify({'error': 'invalid length range'}), 400
    if (n - s) > 0.4 or (e - w) > 0.4:
        return jsonify({'error': 'Zoom in a little so I can suggest routes for a smaller area'}), 400
    min_km, max_km = min_mi / 0.621371, max_mi / 0.621371
    mid_km = (min_km + max_km) / 2
    mid_mi = (min_mi + max_mi) / 2

    cache_key = f"discover:{s:.3f},{w:.3f},{n:.3f},{e:.3f}:{min_mi:.1f}:{max_mi:.1f}"
    cached = cached_response(cache_key, ttl=1800)
    if cached:
        return jsonify(cached)

    try:
        # Higher way cap than the single-anchor builder: discovery needs a fuller
        # network across the whole viewport or connectivity breaks and no long
        # routes form.
        q = (f'[out:json][timeout:45];'
             f'way["highway"~"^(path|footway|track|cycleway|bridleway)$"]'
             f'({s},{w},{n},{e});out geom 2500;')
        net = overpass_query(q, read_timeout=55)
        edges_pts = [
            dict(name=(el.get('tags', {}) or {}).get('name', ''),
                 pts=[{'lat': p['lat'], 'lon': p['lon']} for p in el['geometry']])
            for el in net.get('elements', [])
            if el.get('type') == 'way' and 'geometry' in el
        ]
        if not edges_pts:
            return jsonify({'routes': [], 'count': 0})

        edges, adj = _build_route_graph(edges_pts)
        if not adj:
            return jsonify({'routes': [], 'count': 0})

        # Candidate start nodes: dead-ends (natural trailheads for out-and-backs) first,
        # then junctions (loop starts). Deterministic, bounded so the search stays fast.
        deg = {node: len(nbrs) for node, nbrs in adj.items()}
        deadends = sorted([nd for nd, d in deg.items() if d == 1])[:16]
        junctions = sorted([nd for nd, d in deg.items() if d >= 3])[:10]
        starts = list(dict.fromkeys(deadends + junctions))[:22]

        # Target distances spread across the range so suggestions vary in length.
        span_km = max_km - min_km
        targets = {round(min_km + span_km * f, 2) for f in (0.25, 0.5, 0.75)}

        found = {}
        for start in starts:
            for tgt in targets:
                for mode in ('loop', 'outback'):
                    stem, cycle, km = _search_route(edges, adj, start, tgt, mode, max_expansions=30000)
                    if stem is None and cycle is None:
                        continue
                    total_mi = km * 0.621371
                    if not (min_mi - 0.1 <= total_mi <= max_mi + 0.1):
                        continue
                    sig = frozenset((stem or []) + (cycle or []))
                    if not sig or sig in found:
                        continue
                    geom, names = _route_geometry(edges, stem or [], cycle, start)
                    named = [x for x in names if x]
                    found[sig] = {
                        'mode': mode,
                        'total_mi': round(total_mi, 1),
                        'oneway_mi': round(total_mi / 2, 1) if mode == 'outback' else None,
                        'names': named[:8],
                        'n_segments': len(stem or []) + len(cycle or []),
                        'geometry': geom,
                    }

        # Within each length bucket prefer real named trails, then more segments.
        items = sorted(found.items(),
                       key=lambda kv: (0 if kv[1]['names'] else 1, -kv[1]['n_segments']))
        # Spread suggestions across the length range (buckets) and balance loop vs
        # out-and-back, so the list varies in distance and shows both route types.
        buckets = 8
        picked, used = [], set()
        for b in range(buckets):
            lo = min_mi + (max_mi - min_mi) * b / buckets
            hi = min_mi + (max_mi - min_mi) * (b + 1) / buckets
            cand = [(sig, r) for sig, r in items
                    if sig not in used and lo <= r['total_mi'] <= hi]
            if not cand:
                continue
            n_loop = sum(1 for p in picked if p['mode'] == 'loop')
            pref = 'outback' if n_loop > len(picked) - n_loop else 'loop'
            cand.sort(key=lambda kv: 0 if kv[1]['mode'] == pref else 1)
            sig, r = cand[0]
            picked.append(r)
            used.add(sig)
        if len(picked) < 8:  # sparse range: backfill with the best remaining
            for sig, r in items:
                if sig not in used:
                    picked.append(r)
                    used.add(sig)
                    if len(picked) >= 8:
                        break
        picked.sort(key=lambda r: r['total_mi'])
        result = {'routes': picked, 'count': len(found)}
        cache_response(cache_key, result, ttl=1800)
        return jsonify(result)
    except Exception as ex:
        logger.warning(f'route discover failed: {ex}')
        return jsonify({'error': 'Trail network lookup timed out — try again in a minute'}), 503


@trail_bp.route('/api/trails/lengths', methods=['POST'])
def api_trails_lengths():
    """Batch-measure trail lengths (miles) so the map can filter pins by length.
    Search results carry no length; this fetches geometry for a whole set of
    trails in one Overpass query and sums each one's distance."""
    d = request.get_json(silent=True) or {}
    trails = d.get('trails') or []
    if not trails:
        return jsonify({'lengths': {}})

    way_ids, rel_ids = set(), set()
    for t in trails:
        try:
            oid = int(t.get('osm_id'))
        except (TypeError, ValueError):
            continue
        if t.get('osm_type') == 'relation':
            rel_ids.add(oid)
        else:
            way_ids.add(oid)
            for w in (t.get('way_ids') or []):
                try:
                    way_ids.add(int(w))
                except (TypeError, ValueError):
                    pass
    if not way_ids and not rel_ids:
        return jsonify({'lengths': {}})

    cache_key = ("lengths:" + ",".join(map(str, sorted(way_ids)))
                 + ":" + ",".join(map(str, sorted(rel_ids))))
    cached = cached_response(cache_key, ttl=1800)
    if cached:
        return jsonify(cached)

    q = '[out:json][timeout:90];'
    if rel_ids:
        q += f'relation(id:{",".join(map(str, sorted(rel_ids)))});out body;'
    if way_ids:
        q += f'way(id:{",".join(map(str, sorted(way_ids)))});out geom;'
    if rel_ids:
        q += f'relation(id:{",".join(map(str, sorted(rel_ids)))});way(r);out geom;'

    try:
        data = overpass_query(q, read_timeout=95)
    except Exception as ex:
        logger.warning(f'trail lengths query failed: {ex}')
        return jsonify({'lengths': {}, 'error': 'measure timed out'})

    way_geom, rel_members = {}, {}
    for el in data.get('elements', []):
        if el.get('type') == 'way' and 'geometry' in el:
            way_geom[el['id']] = [{'lat': p['lat'], 'lon': p['lon']} for p in el['geometry']]
        elif el.get('type') == 'relation':
            rel_members[el['id']] = [m['ref'] for m in el.get('members', []) if m.get('type') == 'way']

    lengths = {}
    for t in trails:
        try:
            oid = int(t.get('osm_id'))
        except (TypeError, ValueError):
            continue
        if t.get('osm_type') == 'relation':
            wids = rel_members.get(oid, [])
        else:
            wids = [oid] + [int(w) for w in (t.get('way_ids') or []) if str(w).isdigit()]
        km, seen = 0.0, set()
        for w in wids:
            if w in seen:
                continue
            seen.add(w)
            g = way_geom.get(w)
            if g and len(g) > 1:
                km += _chain_length_km(g)
        if km > 0:
            lengths[f"{t.get('osm_type')}/{oid}"] = round(km * 0.621371, 2)

    result = {'lengths': lengths}
    cache_response(cache_key, result, ttl=1800)
    return jsonify(result)

from math import radians, sin, cos, sqrt, atan2
from flask import Blueprint, request, jsonify

from services.cache import cached_response, cache_response
from services.overpass import overpass_query, CACHE_TTL
from services.usgs import usgs_query_bbox
from services import http_session

trail_bp = Blueprint('trail', __name__)


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
            for seg in elements:
                if 'geometry' in seg:
                    seg_pts = [{'lat': p['lat'], 'lon': p['lon']} for p in seg['geometry']]
                    geometry.extend(seg_pts)
        elif osm_type == 'relation':
            for way in way_els:
                if 'geometry' in way:
                    geometry.extend([{'lat': p['lat'], 'lon': p['lon']} for p in way['geometry']])

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
        access_trail = None
        has_trailhead = False

        if geometry and len(geometry) > 1:
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
                th_elements = th_data.get('elements', [])
                has_trailhead = len(th_elements) > 0

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
            except Exception:
                pass

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

        # Reverse geocode for location context
        if lat and lon:
            try:
                loc = _reverse_geocode(lat, lon)
                result['location'] = loc
            except Exception:
                result['location'] = {}

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

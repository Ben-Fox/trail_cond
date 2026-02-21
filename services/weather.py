import time
from flask import Blueprint, request, jsonify

from services import http_session
from services.cache import cached_response, cache_response

weather_bp = Blueprint('weather', __name__)

_weather_cache = {}
WEATHER_CACHE_TTL = 900  # 15 minutes


@weather_bp.route('/api/weather/grid')
def api_weather_grid():
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

    for i in range(len(lat_list)):
        key = f"grid:{float(lat_list[i]):.2f},{float(lon_list[i]):.2f}"
        if key in _weather_cache and now - _weather_cache[key][0] < WEATHER_CACHE_TTL:
            results.append(_weather_cache[key][1])
        else:
            results.append(None)
            uncached_indices.append(i)

    if uncached_indices:
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

                if condition == 'clear' and elev_ft > 8000 and avg_min < -3:
                    condition = 'icy'
                    reasons.append(f'High elevation ({elev_ft:.0f}ft) + freezing temps')
                elif condition == 'clear' and elev_ft > 6500 and avg_min < -5 and total_precip > 2:
                    condition = 'snowy'
                    reasons.append(f'Elevation {elev_ft:.0f}ft with sub-freezing temps + recent precip')

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


@weather_bp.route('/api/weather/batch')
def api_weather_batch():
    locations = request.args.get('locations', '')
    if not locations:
        return jsonify([])
    pairs = [l.split(',') for l in locations.split('|') if ',' in l]
    now = time.time()
    results = []
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


@weather_bp.route('/api/weather/history')
def api_weather_history():
    lat = request.args.get('lat')
    lon = request.args.get('lon')
    if not lat or not lon:
        return jsonify({'error': 'lat/lon required'}), 400

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

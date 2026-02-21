import time
from flask import Blueprint, request, jsonify

from services import http_session
from services.cache import cached_response, cache_response

weather_bp = Blueprint('weather', __name__)

_weather_cache = {}
WEATHER_CACHE_TTL = 900  # 15 minutes


def _moisture_budget_inference(daily, current=None, elevation=0):
    """
    Moisture Budget Model — simulates day-by-day trail moisture to infer condition.
    
    Walks through 7 days of weather history, adding precipitation and subtracting
    drying based on temperature, solar radiation, evapotranspiration, and wind.
    Returns dict with 'condition' and 'reasons'.
    """
    rain_days = daily.get('rain_sum') or []
    snow_days = daily.get('snowfall_sum') or []
    precip_days = daily.get('precipitation_sum') or []
    temp_max = daily.get('temperature_2m_max') or []
    temp_min = daily.get('temperature_2m_min') or []
    et0_days = daily.get('et0_fao_evapotranspiration') or []
    solar_days = daily.get('shortwave_radiation_sum') or []
    wind_days = daily.get('windspeed_10m_max') or []
    
    n_days = len(rain_days) if rain_days else len(precip_days)
    snow_depth_m = (current or {}).get('snow_depth', 0) or 0
    snow_depth_in = snow_depth_m * 39.37
    current_temp = (current or {}).get('temperature_2m', 10) or 10
    elev_ft = (elevation or 0) * 3.281
    
    reasons = []
    condition = 'clear'
    
    # --- Snow depth check (strongest signal) ---
    if snow_depth_in > 12:
        condition = 'snowy'
        reasons.append(f'{snow_depth_in:.0f}" snow depth on ground')
        if current_temp < -2:
            reasons.append(f'Currently {current_temp:.0f}°C — packed/icy snow likely')
        if elev_ft > 5000:
            reasons.append(f'Elevation: {elev_ft:.0f}ft')
        return {'condition': condition, 'reasons': reasons}
    
    if snow_depth_in > 3:
        if current_temp < 0:
            condition = 'icy'
            reasons.append(f'{snow_depth_in:.0f}" snow + freezing ({current_temp:.0f}°C)')
        else:
            condition = 'snowy'
            reasons.append(f'{snow_depth_in:.0f}" snow on ground')
        if elev_ft > 5000:
            reasons.append(f'Elevation: {elev_ft:.0f}ft')
        return {'condition': condition, 'reasons': reasons}
    
    # --- Moisture budget simulation ---
    moisture = 0.0
    snow_on_ground = 0.0
    total_rain = 0.0
    total_snow = 0.0
    
    for i in range(n_days):
        rain = (rain_days[i] if i < len(rain_days) else 0) or 0
        snow = (snow_days[i] if i < len(snow_days) else 0) or 0
        t_max = (temp_max[i] if i < len(temp_max) and temp_max[i] is not None else 10)
        t_min = (temp_min[i] if i < len(temp_min) and temp_min[i] is not None else 0)
        et0 = (et0_days[i] if i < len(et0_days) and et0_days[i] is not None else 1.5)
        solar = (solar_days[i] if i < len(solar_days) and solar_days[i] is not None else 10)
        wind = (wind_days[i] if i < len(wind_days) and wind_days[i] is not None else 10)
        
        avg_temp = (t_max + t_min) / 2
        total_rain += rain
        total_snow += snow
        
        # Add precipitation
        moisture += rain
        snow_on_ground += snow
        
        # Snowmelt → moisture
        if avg_temp > 2 and snow_on_ground > 0:
            melt_rate = min(snow_on_ground, (avg_temp - 2) * 0.5)
            snow_on_ground -= melt_rate
            moisture += melt_rate * 0.8
        
        # Drying: ET0 base × temperature × solar × wind adjustments
        drying = et0
        
        if avg_temp > 10:
            drying *= 1.0 + (avg_temp - 10) * 0.08
        else:
            drying *= max(0.3, 0.5 + avg_temp * 0.05)
        
        drying *= max(0.5, solar / 15.0)
        drying *= 1.0 + max(0, wind - 10) * 0.02
        
        if elev_ft > 8000:
            drying *= 0.6
        elif elev_ft > 6000:
            drying *= 0.8
        
        moisture = max(0, moisture - drying)
    
    # --- Classify ---
    if snow_on_ground > 5 or total_snow > 8:
        condition = 'snowy'
        reasons.append(f'{total_snow:.1f}cm snow in 7 days')
        if snow_on_ground > 1:
            reasons.append(f'~{snow_on_ground:.0f}cm still on ground (modeled)')
    elif snow_on_ground > 1:
        min_temps_list = [v for v in (temp_min or []) if v is not None]
        avg_min = sum(min_temps_list) / len(min_temps_list) if min_temps_list else 0
        if avg_min < 0:
            condition = 'icy'
            reasons.append(f'Residual snow + freezing temps (avg low {avg_min:.0f}°C)')
        else:
            condition = 'snowy'
            reasons.append(f'Some snow still on trails')
    elif moisture > 12:
        condition = 'muddy'
        reasons.append(f'{total_rain:.1f}mm rain — ground still saturated')
        # Explain WHY it's still wet
        last_et0 = [v for v in et0_days if v is not None]
        if last_et0 and sum(last_et0[-3:]) / min(3, len(last_et0[-3:])) < 2:
            reasons.append('Low evaporation (cool/cloudy) keeping trails wet')
    elif moisture > 5:
        condition = 'wet'
        reasons.append(f'Recent rain ({total_rain:.1f}mm) — trails still damp')
        last_solar = [v for v in solar_days if v is not None]
        if last_solar and last_solar[-1] and last_solar[-1] > 18:
            reasons.append('Sunny conditions helping trails dry')
    elif moisture > 2:
        condition = 'clear'
        if total_rain > 2:
            reasons.append(f'Rain ({total_rain:.1f}mm) mostly dried out')
        else:
            reasons.append('Conditions look good')
    else:
        condition = 'dry'
        if total_rain < 1:
            reasons.append('Minimal precipitation — trails dry')
        else:
            reasons.append(f'{total_rain:.1f}mm rain but warm/sunny drying — trails dry')
    
    if elev_ft > 5000:
        reasons.append(f'Elevation: {elev_ft:.0f}ft')
    if not reasons:
        reasons.append('Conditions look good')
    
    return {'condition': condition, 'reasons': reasons}


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
                'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,rain_sum,shortwave_radiation_sum,et0_fao_evapotranspiration,windspeed_10m_max',
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
                
                inf = _moisture_budget_inference(daily, current, elevation)
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
            'current': 'snow_depth,temperature_2m',
            'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,rain_sum,weathercode,shortwave_radiation_sum,et0_fao_evapotranspiration,windspeed_10m_max',
            'past_days': 7,
            'forecast_days': 1,
            'timezone': 'auto'
        }, timeout=10)

        data = r.json()
        current_weather = data.get('current_weather', {})
        current = data.get('current', {})
        daily = data.get('daily', {})
        elevation = data.get('elevation', 0)

        # Use moisture budget model
        inf = _moisture_budget_inference(daily, current, elevation)
        condition = inf['condition']
        reasons = inf['reasons']

        total_precip = sum(v for v in (daily.get('precipitation_sum') or []) if v)
        total_snow = sum(v for v in (daily.get('snowfall_sum') or []) if v)
        total_rain = sum(v for v in (daily.get('rain_sum') or []) if v)

        COND_COLORS = {'clear': '#2d6a4f', 'dry': '#2d6a4f', 'wet': '#b08968',
                       'muddy': '#7f5539', 'snowy': '#4a90d9', 'icy': '#c44536'}
        color = COND_COLORS.get(condition, '#2d6a4f')

        badge = 'green'
        if condition in ('wet', 'muddy'):
            badge = 'yellow'
        elif condition in ('snowy', 'icy'):
            badge = 'red'

        wx_result = {
            'current': {
                'temp_c': current_weather.get('temperature'),
                'windspeed': current_weather.get('windspeed'),
                'weathercode': current_weather.get('weathercode'),
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

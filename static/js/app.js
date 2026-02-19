// Init map
mainMap = initMap('map');

// Populate states dropdown
const states = ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY'];
const stateSelect = document.getElementById('state-filter');
states.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s; opt.textContent = s;
    stateSelect.appendChild(opt);
});

// Load activities
fetch('/api/activities').then(r=>r.json()).then(data => {
    const sel = document.getElementById('activity-filter');
    (data.activities||[]).forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.id; opt.textContent = a.name;
        sel.appendChild(opt);
    });
});

// Search
document.getElementById('search-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') doSearch();
});

async function doSearch() {
    const q = document.getElementById('search-input').value.trim();
    const state = document.getElementById('state-filter').value;
    const activity = document.getElementById('activity-filter').value;
    
    if (!q && !state) return;
    
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (state) params.set('state', state);
    if (activity) params.set('activity', activity);
    params.set('limit', '30');
    
    const list = document.getElementById('results-list');
    list.innerHTML = '<p class="placeholder-text">Searching...</p>';
    
    try {
        const res = await fetch('/api/search?' + params);
        const data = await res.json();
        renderResults(data.results || [], data.total || 0);
    } catch(e) {
        list.innerHTML = `<p class="error">Search failed: ${e.message}</p>`;
    }
}

function searchNearMe() {
    if (!navigator.geolocation) { alert('Geolocation not supported'); return; }
    navigator.geolocation.getCurrentPosition(async pos => {
        const params = new URLSearchParams({
            lat: pos.coords.latitude,
            lon: pos.coords.longitude,
            radius: 50,
            limit: 30
        });
        const list = document.getElementById('results-list');
        list.innerHTML = '<p class="placeholder-text">Finding nearby...</p>';
        try {
            const res = await fetch('/api/search?' + params);
            const data = await res.json();
            renderResults(data.results || [], data.total || 0);
        } catch(e) {
            list.innerHTML = `<p class="error">Search failed: ${e.message}</p>`;
        }
    }, () => alert('Location access denied'));
}

// Weather code descriptions
const weatherDescs = {
    0:'Clear ☀️', 1:'Mostly Clear 🌤', 2:'Partly Cloudy ⛅', 3:'Overcast ☁️',
    45:'Fog 🌫', 48:'Fog 🌫', 51:'Drizzle 🌦', 53:'Drizzle 🌦', 55:'Drizzle 🌦',
    61:'Rain 🌧', 63:'Rain 🌧', 65:'Heavy Rain 🌧', 71:'Snow 🌨', 73:'Snow 🌨',
    75:'Heavy Snow ❄️', 77:'Snow Grains ❄️', 80:'Showers 🌦', 81:'Showers 🌦',
    82:'Heavy Showers 🌧', 85:'Snow Showers 🌨', 86:'Snow Showers ❄️',
    95:'Thunderstorm ⛈', 96:'Thunderstorm ⛈', 99:'Thunderstorm ⛈'
};

function renderResults(results, total) {
    const list = document.getElementById('results-list');
    if (!results.length) {
        list.innerHTML = '<p class="placeholder-text">No results found. Try a different search.</p>';
        return;
    }
    
    // Render cards immediately with loading weather
    list.innerHTML = `<p class="results-count">${total} results found</p>` +
        results.map(r => {
            const openStatus = r.enabled === false ? 'closed' : 'open';
            const openClass = openStatus === 'closed' ? 'badge-bad' : 'badge-good';
            const openLabel = openStatus === 'closed' ? 'Facility Closed' : 'Facility Open';
            return `
            <div class="result-card conditions-first" onclick="window.location='/facility/${r.id}'" data-facility-id="${r.id}">
                <div class="rc-condition-row" id="weather-${r.id}">
                    <div class="rc-badge rc-badge-loading">⏳ Loading conditions...</div>
                </div>
                <div class="rc-status-row">
                    <span class="badge ${openClass}">${openLabel}</span>
                </div>
                <h3><a href="/facility/${r.id}">${r.name}</a></h3>
                <div class="result-meta">
                    ${r.type || ''}
                    ${r.lat ? ` · ${r.lat.toFixed(2)}, ${r.lon.toFixed(2)}` : ''}
                </div>
            </div>`;
        }).join('');
    
    addMarkers(results);
    
    // Batch fetch weather for all results with lat/lon
    const withCoords = results.filter(r => r.lat && r.lon);
    if (withCoords.length) {
        fetchBatchWeather(withCoords);
    }
}

async function fetchBatchWeather(facilities) {
    const locations = facilities.map(f => `${f.lat},${f.lon}`).join('|');
    try {
        const res = await fetch(`/api/weather/batch?locations=${encodeURIComponent(locations)}`);
        const data = await res.json();
        if (data.results) {
            data.results.forEach((w, i) => {
                if (i < facilities.length) {
                    updateResultWeather(facilities[i].id, w);
                }
            });
        }
    } catch(e) {
        // Silently fail - cards still work without weather
        facilities.forEach(f => {
            const el = document.getElementById(`weather-${f.id}`);
            if (el) el.innerHTML = '<div class="rc-badge rc-badge-unknown">Weather unavailable</div>';
        });
    }
}

function updateResultWeather(facilityId, weather) {
    const el = document.getElementById(`weather-${facilityId}`);
    if (!el) return;
    
    const inf = weather.inference;
    const cur = weather.current;
    
    if (!inf && !cur) {
        el.innerHTML = '<div class="rc-badge rc-badge-unknown">No weather data</div>';
        return;
    }
    
    const levelClass = inf ? {green:'rc-badge-green', yellow:'rc-badge-yellow', red:'rc-badge-red'}[inf.level] || 'rc-badge-unknown' : 'rc-badge-unknown';
    const prediction = inf ? inf.prediction : '';
    const temp = cur ? `${Math.round(cur.temperature)}°F` : '';
    const weatherLabel = cur ? (weatherDescs[cur.weathercode] || '') : '';
    const reasons = inf && inf.reasons.length ? inf.reasons.join(' · ') : '';
    
    el.innerHTML = `
        <div class="rc-badge ${levelClass}">
            ${prediction}
        </div>
        ${temp ? `<span class="rc-temp">${temp}</span>` : ''}
        ${weatherLabel ? `<span class="rc-weather-label">${weatherLabel}</span>` : ''}
        ${reasons ? `<div class="rc-reasons">${reasons}</div>` : ''}
    `;
}

function stripHtml(html) {
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    return tmp.textContent.substring(0, 200);
}

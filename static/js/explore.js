const STATES = ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY'];
const WMO = {0:'Clear',1:'Mostly Clear',2:'Partly Cloudy',3:'Overcast',45:'Foggy',48:'Fog',51:'Light Drizzle',53:'Drizzle',55:'Heavy Drizzle',61:'Light Rain',63:'Rain',65:'Heavy Rain',71:'Light Snow',73:'Snow',75:'Heavy Snow',77:'Snow Grains',80:'Light Showers',81:'Showers',82:'Heavy Showers',85:'Snow Showers',86:'Heavy Snow Showers',95:'Thunderstorm',96:'Hail Storm',99:'Heavy Hail'};

let map, markers = [], allResults = [], currentFilter = 'all';

document.addEventListener('DOMContentLoaded', () => {
    const sel = document.getElementById('search-state');
    STATES.forEach(s => { const o = document.createElement('option'); o.value = s; o.textContent = s; sel.appendChild(o); });
    map = L.map('explore-map').setView([39.5, -98.5], 4);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {attribution:'OpenStreetMap'}).addTo(map);
    document.getElementById('search-query').addEventListener('keydown', e => { if(e.key==='Enter') doSearch(); });
});

function doSearch() {
    const q = document.getElementById('search-query').value;
    const st = document.getElementById('search-state').value;
    const params = new URLSearchParams();
    if(q) params.set('query', q);
    if(st) params.set('state', st);
    params.set('limit', '25');
    fetchResults('/api/search?' + params.toString());
}

function nearMe() {
    if(!navigator.geolocation) return alert('Geolocation not supported');
    navigator.geolocation.getCurrentPosition(pos => {
        const params = new URLSearchParams({lat: pos.coords.latitude, lon: pos.coords.longitude, radius: 50, limit: 25});
        fetchResults('/api/search?' + params.toString());
        map.setView([pos.coords.latitude, pos.coords.longitude], 9);
    }, () => alert('Could not get location'));
}

function fetchResults(url) {
    document.getElementById('results-loading').style.display = 'block';
    document.getElementById('results-empty').style.display = 'none';
    fetch(url).then(r=>r.json()).then(data => {
        allResults = data.results || [];
        renderResults();
        fetchBatchWeather();
    }).catch(() => {
        document.getElementById('results-loading').style.display = 'none';
        document.getElementById('results-empty').style.display = 'block';
        document.getElementById('results-empty').textContent = 'Error fetching results.';
    });
}

function fetchBatchWeather() {
    const locs = allResults.filter(r=>r.lat&&r.lon).map(r=>`${r.id},${r.lat},${r.lon}`).join(';');
    if(!locs) return;
    fetch('/api/weather/batch?locations='+encodeURIComponent(locs)).then(r=>r.json()).then(weather => {
        allResults.forEach(r => { if(weather[r.id]) r.weather = weather[r.id]; });
        renderResults();
    });
}

function getTypeColor(type) {
    const t = (type||'').toLowerCase();
    if(t.includes('campground')) return '#52b788';
    if(t.includes('trailhead') || t.includes('trail')) return '#e09f3e';
    if(t.includes('day use') || t.includes('picnic')) return '#457b9d';
    return '#6c757d';
}

function renderResults() {
    const list = document.getElementById('results-list');
    document.getElementById('results-loading').style.display = 'none';
    markers.forEach(m => map.removeLayer(m));
    markers = [];

    const filtered = currentFilter === 'all' ? allResults : allResults.filter(r => (r.type||'').toLowerCase().includes(currentFilter));

    let html = '';
    if(!filtered.length) {
        html = '<div style="text-align:center;padding:2rem;color:var(--gray)">No results found.</div>';
    }
    const bounds = [];
    filtered.forEach(r => {
        const w = r.weather;
        const color = w ? w.inference.color : 'gray';
        const condClass = 'cond-' + color;
        const badgeClass = 'badge-' + color;
        const condText = w ? w.inference.conditions[0] : 'Weather data loading...';
        const tempText = w ? `${Math.round(w.temp)}°F` : '';
        const weatherDesc = w ? (WMO[w.weathercode] || '') : '';
        const typeColor = getTypeColor(r.type);

        html += `<div class="result-card ${condClass}">
            <span class="result-badge ${badgeClass}">${condText}</span>
            <div class="result-weather">${tempText ? `<strong>${tempText}</strong> ${weatherDesc}` : '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>'}</div>
            <span style="font-size:.8rem;color:${r.enabled?'var(--green-light)':'var(--red)'};font-weight:700">${r.enabled?'Open':'Closed'}</span>
            <h4><a href="/facility/${r.id}">${r.name}</a><span class="type-badge" style="background:${typeColor}20;color:${typeColor}">${r.type||'Recreation'}</span></h4>
            <div class="result-meta">${r.state||''}</div>
            <a href="/facility/${r.id}" class="btn btn-outline btn-sm" style="margin-top:.5rem">View Details</a>
        </div>`;

        if(r.lat && r.lon) {
            const m = L.circleMarker([r.lat, r.lon], {radius: 8, fillColor: typeColor, color: '#fff', weight: 2, fillOpacity: .8})
                .bindPopup(`<strong>${r.name}</strong><br>${r.type||''}<br><a href="/facility/${r.id}">Details</a>`)
                .addTo(map);
            markers.push(m);
            bounds.push([r.lat, r.lon]);
        }
    });
    list.innerHTML = html;
    if(bounds.length) map.fitBounds(bounds, {padding: [30,30], maxZoom: 10});
}

function setFilter(f, btn) {
    currentFilter = f;
    document.querySelectorAll('.filter-bar .btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderResults();
}

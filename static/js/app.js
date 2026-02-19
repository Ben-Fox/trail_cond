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

function renderResults(results, total) {
    const list = document.getElementById('results-list');
    if (!results.length) {
        list.innerHTML = '<p class="placeholder-text">No results found. Try a different search.</p>';
        return;
    }
    list.innerHTML = `<p style="font-size:0.85rem;color:#636e72;margin-bottom:0.5rem;">${total} results found</p>` +
        results.map(r => `
            <div class="result-card" onclick="window.location='/facility/${r.id}'">
                <h3><a href="/facility/${r.id}">${r.name}</a></h3>
                <p>${stripHtml(r.description || '')}</p>
                <div class="result-meta">
                    ${r.type || ''} ${r.ada === 'Yes' ? '♿' : ''}
                    ${r.lat ? `📍 ${r.lat.toFixed(2)}, ${r.lon.toFixed(2)}` : ''}
                </div>
            </div>
        `).join('');
    
    addMarkers(results);
}

function stripHtml(html) {
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    return tmp.textContent.substring(0, 200);
}

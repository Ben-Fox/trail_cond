const WMO = {0:'Clear',1:'Mostly Clear',2:'Partly Cloudy',3:'Overcast',45:'Foggy',48:'Fog',51:'Light Drizzle',53:'Drizzle',55:'Heavy Drizzle',61:'Light Rain',63:'Rain',65:'Heavy Rain',71:'Light Snow',73:'Snow',75:'Heavy Snow',77:'Snow Grains',80:'Light Showers',81:'Showers',82:'Heavy Showers',85:'Snow Showers',86:'Heavy Snow Showers',95:'Thunderstorm',96:'Hail Storm',99:'Heavy Hail'};
let map, trailLayer, trailsOn = false, facilityData = null;

document.addEventListener('DOMContentLoaded', () => {
    map = L.map('facility-map').setView([39.5, -98.5], 4);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {attribution:'OpenStreetMap'}).addTo(map);
    trailLayer = L.layerGroup();
    loadFacility();
});

function loadFacility() {
    fetch(`/api/facility/${FACILITY_ID}`).then(r=>r.json()).then(data => {
        facilityData = data;
        document.getElementById('fac-name').textContent = data.name;
        document.getElementById('fac-type').textContent = data.type || '';
        document.title = data.name + ' — TrailCondish';
        document.getElementById('fac-description').innerHTML = data.description || '<em>No description available.</em>';
        if(data.directions) document.getElementById('fac-directions').innerHTML = '<strong>Directions:</strong> ' + data.directions;
        if(data.activities && data.activities.length) {
            document.getElementById('fac-activities-block').style.display = 'block';
            document.getElementById('fac-activities').innerHTML = data.activities.map(a=>`<span class="activity-tag">${a}</span>`).join('');
        }
        if(data.photos && data.photos.length) {
            document.getElementById('fac-photos-block').style.display = 'block';
            document.getElementById('fac-photos').innerHTML = data.photos.map(p=>`<img src="${p.url}" alt="${p.title}" loading="lazy">`).join('');
        }
        if(data.phone || data.email) {
            document.getElementById('fac-contact-block').style.display = 'block';
            let c = '';
            if(data.phone) c += `<p>Phone: ${data.phone}</p>`;
            if(data.email) c += `<p>Email: ${data.email}</p>`;
            document.getElementById('fac-contact').innerHTML = c;
        }
        if(data.lat && data.lon) {
            map.setView([data.lat, data.lon], 12);
            L.marker([data.lat, data.lon]).addTo(map).bindPopup(`<strong>${data.name}</strong>`).openPopup();
            loadWeather(data.lat, data.lon);
        }
        renderReports(data.reports || []);
    });
}

function loadWeather(lat, lon) {
    Promise.all([
        fetch(`/api/weather/${lat}/${lon}`).then(r=>r.json()),
        fetch(`/api/weather/history/${lat}/${lon}`).then(r=>r.json())
    ]).then(([current, history]) => {
        const cw = current.current_weather || {};
        const inf = history.condition_inference || {};
        let html = `<div class="weather-current">
            <span class="weather-temp">${Math.round(cw.temperature||0)}°F</span>
            <span>${WMO[cw.weathercode]||'Unknown'}, Wind ${Math.round(cw.windspeed||0)} mph</span>
        </div>`;
        if(inf.color) {
            html += `<div class="condition-box cond-${inf.color}">
                <h4>7-Day Trail Condition Inference</h4>
                <ul>${(inf.conditions||[]).map(c=>`<li>${c}</li>`).join('')}</ul>
                <div class="reasoning">${inf.reasoning||''}</div>
            </div>`;
        }
        document.getElementById('weather-content').innerHTML = html;
    }).catch(() => {
        document.getElementById('weather-content').innerHTML = '<p style="color:var(--gray)">Weather data unavailable.</p>';
    });
}

function toggleTrails() {
    const tog = document.getElementById('trail-toggle');
    trailsOn = !trailsOn;
    tog.classList.toggle('on', trailsOn);
    if(trailsOn) {
        const wmLayer = L.tileLayer('https://tile.waymarkedtrails.org/hiking/{z}/{x}/{y}.png', {opacity: .7});
        trailLayer.addLayer(wmLayer);
        trailLayer.addTo(map);
        if(facilityData && facilityData.lat) {
            fetch(`/api/trails/nearby?lat=${facilityData.lat}&lon=${facilityData.lon}`).then(r=>r.json()).then(trails => {
                trails.forEach(t => {
                    if(t.geojson) {
                        const gl = L.geoJSON(t.geojson, {style: {color: '#e09f3e', weight: 3}}).bindPopup(t.name);
                        trailLayer.addLayer(gl);
                    }
                });
            });
        }
    } else {
        trailLayer.clearLayers();
    }
}

function renderReports(reports) {
    const el = document.getElementById('reports-list');
    if(!reports.length) { el.innerHTML = '<p style="color:var(--gray)">No reports yet. Be the first!</p>'; return; }
    el.innerHTML = reports.map(r => `<div class="report-item">
        <div class="report-header">
            <span><span class="report-status" style="color:${r.status==='Great'||r.status==='Good'?'var(--green)':r.status==='Poor'||r.status==='Closed'?'var(--red)':'var(--yellow)'}">${r.status}</span>
            ${r.trail_condition?` &mdash; ${r.trail_condition}`:''} ${r.road_access?` &mdash; Road: ${r.road_access}`:''}</span>
            <span style="font-size:.8rem;color:var(--gray)">${r.date_visited||r.created_at||''}</span>
        </div>
        <p style="font-size:.9rem">${r.general_notes||''}</p>
        <div class="vote-btns">
            <button class="vote-btn" onclick="vote(${r.id},'up',this)"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg> ${r.upvotes||0}</button>
            <button class="vote-btn" onclick="vote(${r.id},'down',this)"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg> ${r.downvotes||0}</button>
        </div>
    </div>`).join('');
}

function vote(id, type, btn) {
    fetch(`/api/reports/${id}/vote`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vote_type:type})})
    .then(r => { if(r.ok) { btn.style.color='var(--green)'; loadFacility(); } else alert('Already voted'); });
}

document.getElementById('report-form').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = {
        facility_name: facilityData ? facilityData.name : '',
        status: fd.get('status'),
        trail_condition: fd.get('trail_condition') || '',
        road_access: fd.get('road_access') || '',
        date_visited: fd.get('date_visited') || '',
        general_notes: fd.get('general_notes') || ''
    };
    fetch(`/api/facility/${FACILITY_ID}/reports`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)})
    .then(r=>r.json()).then(() => {
        document.getElementById('report-success').style.display = 'block';
        e.target.reset();
        loadFacility();
    });
});

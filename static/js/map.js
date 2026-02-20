let mainMap = null;
let markers = [];
let waymarkedLayer = null;
let trailPolylines = [];

// Colored circle markers for facility types
const facilityColors = {
    'Campground': '#28a745',
    'Trailhead': '#fd7e14',
    'Day Use Area': '#007bff',
};
const defaultColor = '#6c757d';

function getFacilityColor(type) {
    if (!type) return defaultColor;
    const t = type.toLowerCase();
    if (t.includes('campground')) return facilityColors['Campground'];
    if (t.includes('trailhead')) return facilityColors['Trailhead'];
    if (t.includes('day use')) return facilityColors['Day Use Area'];
    return defaultColor;
}

function createColoredMarker(lat, lon, type) {
    const color = getFacilityColor(type);
    return L.circleMarker([lat, lon], {
        radius: 9,
        fillColor: color,
        color: '#fff',
        weight: 2,
        opacity: 1,
        fillOpacity: 0.85
    });
}

function initMap(elementId, lat, lon, zoom) {
    const map = L.map(elementId).setView([lat || 39.8, lon || -98.5], zoom || 4);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 18
    }).addTo(map);
    return map;
}

function addWaymarkedOverlay(map) {
    if (waymarkedLayer) return waymarkedLayer;
    waymarkedLayer = L.tileLayer('https://tile.waymarkedtrails.org/hiking/{z}/{x}/{y}.png', {
        attribution: '&copy; <a href="https://waymarkedtrails.org">Waymarked Trails</a>',
        maxZoom: 18,
        opacity: 0.7
    });
    waymarkedLayer.addTo(map);
    return waymarkedLayer;
}

function removeWaymarkedOverlay(map) {
    if (waymarkedLayer) {
        map.removeLayer(waymarkedLayer);
        waymarkedLayer = null;
    }
}

function clearTrailPolylines(map) {
    trailPolylines.forEach(p => map.removeLayer(p));
    trailPolylines = [];
}

async function loadNearbyTrails(map, lat, lon, facilityName) {
    clearTrailPolylines(map);
    // Search by facility name
    const queries = [facilityName];
    const trails = [];
    for (const q of queries) {
        try {
            const res = await fetch(`https://hiking.waymarkedtrails.org/api/v1/list/search?query=${encodeURIComponent(q)}&limit=5`);
            if (res.ok) {
                const data = await res.json();
                if (data.results) trails.push(...data.results);
            }
        } catch(e) { /* silent */ }
    }
    // Also search by bbox around the point
    try {
        const delta = 0.15;
        const bbox = `${lon-delta},${lat-delta},${lon+delta},${lat+delta}`;
        const res = await fetch(`https://hiking.waymarkedtrails.org/api/v1/list/by_area?bbox=${bbox}&limit=10`);
        if (res.ok) {
            const data = await res.json();
            if (data.results) trails.push(...data.results);
        }
    } catch(e) { /* silent */ }

    // Deduplicate by id
    const seen = new Set();
    const unique = trails.filter(t => { if (seen.has(t.id)) return false; seen.add(t.id); return true; });

    // Load geometry for each trail
    for (const trail of unique.slice(0, 8)) {
        try {
            const res = await fetch(`https://hiking.waymarkedtrails.org/api/v1/details/route/${trail.id}/geometry/geojson`);
            if (res.ok) {
                const geojson = await res.json();
                const polyline = L.geoJSON(geojson, {
                    style: { color: '#e63946', weight: 3, opacity: 0.7 }
                }).addTo(map);
                polyline._trailData = trail;
                polyline.bindPopup(`<strong>${trail.name || 'Trail'}</strong>`);
                trailPolylines.push(polyline);
            }
        } catch(e) { /* silent */ }
    }

    return unique;
}

function clearMarkers() {
    markers.forEach(m => mainMap.removeLayer(m));
    markers = [];
}

function addMarkers(results) {
    if (!mainMap) return;
    clearMarkers();
    const bounds = [];
    results.forEach(r => {
        if (!r.lat || !r.lon) return;
        const marker = createColoredMarker(r.lat, r.lon, r.type);
        marker.addTo(mainMap);
        const typeLabel = r.type ? `<em style="color:#888;font-size:0.85em">${r.type}</em><br>` : '';
        marker.bindPopup(`<strong><a href="/facility/${r.id}">${r.name}</a></strong><br>${typeLabel}${r.description || ''}`);
        markers.push(marker);
        bounds.push([r.lat, r.lon]);
    });
    if (bounds.length) {
        mainMap.fitBounds(bounds, { padding: [30, 30], maxZoom: 12 });
    }
}

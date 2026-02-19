let mainMap = null;
let markers = [];

function initMap(elementId, lat, lon, zoom) {
    const map = L.map(elementId).setView([lat || 39.8, lon || -98.5], zoom || 4);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors',
        maxZoom: 18
    }).addTo(map);
    return map;
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
        const marker = L.marker([r.lat, r.lon]).addTo(mainMap);
        marker.bindPopup(`<strong><a href="/facility/${r.id}">${r.name}</a></strong><br>${r.description || ''}`);
        markers.push(marker);
        bounds.push([r.lat, r.lon]);
    });
    if (bounds.length) {
        mainMap.fitBounds(bounds, { padding: [30, 30], maxZoom: 12 });
    }
}

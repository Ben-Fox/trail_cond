const CACHE_NAME = 'trailcondish-v3';
const TILE_CACHE = 'trailcondish-tiles-v2';
const MAX_TILE_CACHE = 2000; // max cached tiles

// Static assets to pre-cache
const PRECACHE = [
    '/',
    '/explore',
    '/static/style.css',
    '/static/manifest.json',
    '/static/icon.svg',
];

// Tile URL patterns to cache
const TILE_PATTERNS = [
    'basemaps.cartocdn.com',
    'tile.opentopomap.org',
    'arcgisonline.com',
    'tile.thunderforest.com',
    'tile.waymarkedtrails.org',
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(PRECACHE))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME && k !== TILE_CACHE).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});

function isTileRequest(url) {
    return TILE_PATTERNS.some(p => url.includes(p));
}

self.addEventListener('fetch', event => {
    const url = event.request.url;

    if (isTileRequest(url)) {
        // Tile requests: cache-first, fall back to network
        event.respondWith(
            caches.open(TILE_CACHE).then(cache =>
                cache.match(event.request).then(cached => {
                    if (cached) return cached;
                    return fetch(event.request).then(response => {
                        if (response.ok) {
                            cache.put(event.request, response.clone());
                        }
                        return response;
                    }).catch(() => cached);
                })
            )
        );
    } else if (event.request.method === 'GET' && !url.includes('/api/')) {
        // Static assets: network-first, fall back to cache
        event.respondWith(
            fetch(event.request).then(response => {
                if (response.ok) {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                }
                return response;
            }).catch(() => caches.match(event.request))
        );
    }
});

const CACHE_NAME = 'trailcondish-v7';
const TILE_CACHE = 'trailcondish-tiles-v4';
const API_CACHE = 'trailcondish-api-v1';
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

// API paths to cache with stale-while-revalidate (weather doesn't change fast)
const SWR_API_PATTERNS = [
    '/api/weather/grid',
    '/api/weather/history',
    '/api/airquality',
    '/api/tiles/conditions/',
    '/api/tiles/airquality/',
];
const SWR_MAX_AGE_MS = 15 * 60 * 1000; // 15 min — serve stale, revalidate in background

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
            Promise.all(keys.filter(k => k !== CACHE_NAME && k !== TILE_CACHE && k !== API_CACHE).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});

function isSWRApi(url) {
    return SWR_API_PATTERNS.some(p => url.includes(p));
}

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
    } else if (event.request.method === 'GET' && isSWRApi(url)) {
        // API requests: stale-while-revalidate
        // Serve cached instantly, update cache in background
        event.respondWith(
            caches.open(API_CACHE).then(cache =>
                cache.match(event.request).then(cached => {
                    const fetchPromise = fetch(event.request).then(response => {
                        if (response.ok) {
                            cache.put(event.request, response.clone());
                        }
                        return response;
                    }).catch(() => cached);
                    
                    // Return cached immediately if available, otherwise wait for network
                    return cached || fetchPromise;
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

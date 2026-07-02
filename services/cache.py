import time
import threading

# Thread-safe, bounded in-memory API response cache.
# All access to _api_cache goes through _lock so concurrent gthread workers
# (and the ThreadPoolExecutors in usgs/trail) cannot mutate the dict while
# another thread iterates it during pruning ("dictionary changed size" crash).
_api_cache = {}
_lock = threading.Lock()
API_CACHE_TTL = 300  # 5 minutes
_MAX_ENTRIES = 500


def cached_response(key, ttl=API_CACHE_TTL):
    """Return a cached API response if present and fresh, else None."""
    now = time.time()
    with _lock:
        entry = _api_cache.get(key)
    if entry is not None:
        ts, data = entry
        if now - ts < ttl:
            return data
    return None


def cache_response(key, data, ttl=API_CACHE_TTL):
    """Store an API response, pruning to stay under a hard entry cap."""
    now = time.time()
    with _lock:
        _api_cache[key] = (now, data)
        if len(_api_cache) > _MAX_ENTRIES:
            # Drop expired entries first.
            stale = [k for k, (ts, _) in _api_cache.items() if now - ts >= ttl]
            for k in stale:
                _api_cache.pop(k, None)
            # If still over the cap, evict the oldest entries.
            if len(_api_cache) > _MAX_ENTRIES:
                overflow = len(_api_cache) - _MAX_ENTRIES
                oldest = sorted(_api_cache.items(), key=lambda kv: kv[1][0])[:overflow]
                for k, _ in oldest:
                    _api_cache.pop(k, None)
    return data

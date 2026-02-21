import time

# API response cache
_api_cache = {}
API_CACHE_TTL = 300  # 5 minutes


def cached_response(key, ttl=API_CACHE_TTL):
    """Check if a cached API response exists and is fresh."""
    now = time.time()
    if key in _api_cache:
        ts, data = _api_cache[key]
        if now - ts < ttl:
            return data
    return None


def cache_response(key, data, ttl=API_CACHE_TTL):
    """Store an API response in cache."""
    _api_cache[key] = (time.time(), data)
    if len(_api_cache) > 500:
        cutoff = time.time() - ttl
        stale = [k for k, (ts, _) in _api_cache.items() if ts < cutoff]
        for k in stale:
            del _api_cache[k]
    return data

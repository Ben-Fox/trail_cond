# Trail Condish — Scaling Roadmap

## Current Architecture (MVP)
- **Server:** Single machine, Gunicorn 4 workers (gthread)
- **Caching:** In-memory Python dicts (tiles 30min, weather 15min, API responses 5-30min)
- **Trail Data:** Live Overpass API queries per search
- **Database:** SQLite (WAL mode) for community reports
- **Weather/AQI:** Open-Meteo free tier (10k calls/day, 300k/month)

---

## Scaling Tiers

### Tier 1: 1-100 Users (Current — Free)
- **Status:** ✅ No changes needed
- **API Usage:** ~1,200 calls/day worst case
- **Bottleneck:** None
- **Cost:** $0

### Tier 2: 100-500 Users
- **Bottleneck:** Open-Meteo free tier daily limit (10k)
- **TODO:**
  - [ ] Increase tile cache TTL to 60 min (weather doesn't change that fast)
  - [ ] Increase weather history cache to 60 min
  - [ ] Add request deduplication — if two users trigger the same uncached tile simultaneously, only one API call fires
  - [ ] Pre-generate tiles for top 20 hiking regions during off-peak (2-5am)
- **Cost:** $0 (optimizations keep us under free tier)

### Tier 3: 500-1,000 Users
- **Bottleneck:** Overpass API rate limits on trail searches + Open-Meteo free tier
- **TODO:**
  - [ ] **Cache trail data locally in SQLite/PostgreSQL** — store Overpass results with a TTL (trails don't change often, cache for 24-72hr). This is the biggest architectural win.
  - [ ] Upgrade Open-Meteo to Standard plan ($29/mo, 1M calls/month)
  - [ ] Move tile cache from in-memory dicts to **Redis** — survives restarts, shared across workers
  - [ ] Add cache warming job: nightly batch-fetch weather for all cached trail locations
- **Cost:** ~$29/mo (Open-Meteo) + Redis memory

### Tier 4: 1,000-5,000 Users
- **Bottleneck:** Single server CPU (tile rendering), SQLite write contention, Overpass
- **TODO:**
  - [ ] **Pre-render tile pyramid** — generate all condition/AQ tiles for US zoom 5-12 on a schedule (every 30-60 min cron). Serve as static files. Eliminates per-request rendering entirely.
  - [ ] Migrate SQLite → **PostgreSQL** (handles concurrent writes from reports/votes)
  - [ ] Add **CDN** (Cloudflare) in front of tile endpoints — tiles are perfect CDN content (cacheable, static for their TTL)
  - [ ] Upgrade Open-Meteo to Professional plan (~$99/mo, 5M calls/month)
  - [ ] Build local trail index — periodic full Overpass dump of US trails into PostGIS, search against local DB instead of live Overpass
- **Cost:** ~$99/mo (Open-Meteo) + PostgreSQL hosting + CDN (Cloudflare free tier works)

### Tier 5: 5,000-50,000 Users
- **Bottleneck:** Single server capacity, API costs
- **TODO:**
  - [ ] **Horizontal scaling** — multiple app servers behind a load balancer (nginx/Cloudflare)
  - [ ] Redis cluster for shared cache
  - [ ] **Self-host Open-Meteo** — it's open source! Run our own instance, unlimited calls, ~$50-100/mo VPS
  - [ ] Tile rendering as a separate background service (not in web workers)
  - [ ] Consider **MapLibre GL JS** migration — vector tiles rendered client-side, massively reduces server tile rendering load
  - [ ] PostGIS with spatial indexes for trail search (sub-10ms queries)
  - [ ] Rate limiting per IP to prevent abuse
- **Cost:** ~$200-500/mo infrastructure

### Tier 6: 50,000+ Users
- **Bottleneck:** Architecture fundamentals
- **TODO:**
  - [ ] **Vector tiles** (MapLibre + Protobuf tiles) — client renders everything, server just serves data
  - [ ] Microservices split: trail API, weather API, tile server, report service
  - [ ] Managed PostgreSQL (RDS/Cloud SQL)
  - [ ] Kubernetes or similar container orchestration
  - [ ] Global CDN with edge caching
  - [ ] Consider native mobile apps (React Native) for better offline/caching
- **Cost:** $1,000+/mo (but revenue should be significant at this scale)

---

## Architecture Decisions for Easy Scaling

### What's Already Good ✅
- **Modular Flask blueprints** — each service (weather, search, trails, AQ, tiles) is its own module. Can be split into separate services later without rewriting logic.
- **Cache abstraction** — `services/cache.py` centralizes caching. Swap in-memory → Redis by changing one file.
- **Tile-based overlays** — standard `{z}/{x}/{y}.png` pattern. Drop a CDN in front, or swap to pre-rendered static files, with zero frontend changes.
- **Weather model separated from rendering** — `_moisture_budget_inference()` and `_infer_condition()` are pure functions. Can run anywhere.

### What Needs Refactoring at Scale ⚠️
1. **In-memory caches → Redis** (Tier 3)
   - Currently: `_tile_cache = {}`, `_weather_cache = {}`, `_aq_tile_cache = {}`
   - Fix: Replace with Redis GET/SET. Same interface, shared across workers.
   - Effort: ~2 hours

2. **Live Overpass queries → Local trail DB** (Tier 3-4)
   - Currently: Every search hits Overpass API live
   - Fix: Periodic import job pulls US trails from Overpass into PostGIS. Search queries hit local DB.
   - Effort: ~1-2 days
   - Biggest single scaling win

3. **On-demand tile rendering → Pre-rendered tiles** (Tier 4)
   - Currently: Tiles rendered per-request with PIL
   - Fix: Background cron generates tile pyramid every 30-60 min. Serve as static files via nginx/CDN.
   - Effort: ~4 hours

4. **SQLite → PostgreSQL** (Tier 4)
   - Currently: Single-file SQLite, fine for reads but limited concurrent writes
   - Fix: PostgreSQL, connection pooling
   - Effort: ~2-3 hours (mostly config, queries are standard SQL)

---

## Quick Wins Available Now
- [ ] Bump tile cache TTL: 30min → 60min (one-line change, halves API calls)
- [ ] Add `Cache-Control: public, max-age=1800` to weather/AQ API responses for browser caching
- [ ] Gzip tile responses (already have flask-compress, verify it covers image/png)
- [ ] Add ETag headers to tile responses for conditional requests

---

## Cost Projection

| Users/Day | Open-Meteo | Hosting | CDN | Total/mo |
|-----------|-----------|---------|-----|----------|
| 100       | Free      | Free    | -   | $0       |
| 500       | Free      | Free    | -   | $0       |
| 1,000     | $29       | Free    | -   | $29      |
| 5,000     | $99       | $50     | Free| $149     |
| 10,000    | Self-host | $100    | Free| $200     |
| 50,000    | Self-host | $500    | $20 | $520     |

Open-Meteo self-hosting eliminates the largest variable cost.
Cloudflare free tier CDN handles massive traffic for static tiles.

---

*Last updated: 2026-02-21*

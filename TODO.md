# Trail Condish TODO

Future work, not yet started. Each item is safe to defer.

## Air Quality

### Add OpenAQ real-station data (later)
Right now the air-quality overlay uses Open-Meteo (a free, no-key global CAMS model,
~25 km resolution). It has continuous coverage everywhere but is a model, not physical
sensors, so it can differ slightly from a specific ground monitor.

Upgrade: pull real reporting monitoring-station readings from **OpenAQ** (free, nonprofit
aggregator of EPA + global sensors; now requires a free API key) and blend them with the
model.
- Pull all stations reporting in the viewport, plus the model grid as fallback/fill.
- Interpolate between them. Keep IDW when data is sparse; use kriging where stations are
  dense enough (the IDW/kriging hybrid we discussed). Compute onto a cached grid so the
  map stays fast; one-time cold cost on first view of an area.
- Key handling: register a free OpenAQ key, keep it server-side only (Python), cache
  aggressively to respect the free-tier rate limits.
- **Needs Ben's sign-off**: adds a new data source / server config.

## Air Quality / Map perf (nice-to-have follow-ups)
- Parallelize or precache the cold upstream fetch on first view of a new area (currently
  ~1-2 s cold, then cached) to remove the one-time lag.
- `disableClusteringAtZoom` on the marker cluster to stop re-cluster churn at high zoom.

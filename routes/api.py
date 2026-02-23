import logging

logger = logging.getLogger(__name__)

# Core modules (app won't work without these)
from services.search import search_bp
from services.trail import trail_bp
from services.weather import weather_bp
from services.reports import reports_bp

CORE_BLUEPRINTS = [search_bp, trail_bp, weather_bp, reports_bp]

# Optional modules — each loads independently so a failure in one
# doesn't prevent the app from starting
OPTIONAL_BLUEPRINTS = []

def _try_import(module_path, bp_name):
    """Safely import an optional blueprint. Returns None on failure."""
    try:
        import importlib
        mod = importlib.import_module(module_path)
        bp = getattr(mod, bp_name)
        OPTIONAL_BLUEPRINTS.append(bp)
        return bp
    except Exception as e:
        logger.warning(f"Optional module {module_path} failed to load: {e}")
        return None

_try_import('services.elevation', 'elevation_bp')
_try_import('services.condtiles', 'condtiles_bp')
_try_import('services.airquality', 'airquality_bp')
_try_import('services.streams', 'streams_bp')
_try_import('services.npsalerts', 'npsalerts_bp')

ALL_BLUEPRINTS = CORE_BLUEPRINTS + OPTIONAL_BLUEPRINTS

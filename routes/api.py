from services.search import search_bp
from services.trail import trail_bp
from services.elevation import elevation_bp
from services.weather import weather_bp
from services.reports import reports_bp
from services.condtiles import condtiles_bp
from services.airquality import airquality_bp
from services.streams import streams_bp
from services.npsalerts import npsalerts_bp

ALL_BLUEPRINTS = [search_bp, trail_bp, elevation_bp, weather_bp, reports_bp, condtiles_bp, airquality_bp, streams_bp, npsalerts_bp]

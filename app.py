import logging
from flask import Flask, request, jsonify
from flask_compress import Compress
from database import init_db
from routes.pages import pages_bp
from routes.api import ALL_BLUEPRINTS

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Gzip/Brotli compression
Compress(app)
app.config['COMPRESS_ALGORITHM'] = ['br', 'gzip']
app.config['COMPRESS_MIMETYPES'] = [
    'text/html', 'text/css', 'text/javascript', 'application/javascript',
    'application/json', 'image/svg+xml'
]
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400

# Register blueprints
app.register_blueprint(pages_bp)
for bp in ALL_BLUEPRINTS:
    app.register_blueprint(bp)


# Global error handlers — prevent any single module crash from killing the app
@app.errorhandler(500)
def handle_500(e):
    logger.error(f"Internal server error: {e}")
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Internal server error'}), 500
    return "Something went wrong. Please try again.", 500

@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found'}), 404
    return "Page not found.", 404

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception on {request.path}: {e}", exc_info=True)
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Service temporarily unavailable'}), 500
    return "Something went wrong. Please try again.", 500


@app.after_request
def add_cache_headers(response):
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400'
    elif request.path.startswith('/api/tiles/'):
        response.headers['Cache-Control'] = 'public, max-age=1800, stale-while-revalidate=3600'
    elif request.path.startswith('/api/weather/') or request.path.startswith('/api/airquality'):
        response.headers['Cache-Control'] = 'public, max-age=300, stale-while-revalidate=900'
    elif request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'public, max-age=60'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response


# Initialize DB on import
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8095, debug=False, threaded=True)

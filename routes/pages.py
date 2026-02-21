from flask import Blueprint, render_template

pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
def index():
    return render_template('index.html')


@pages_bp.route('/explore')
def explore():
    return render_template('explore.html')


@pages_bp.route('/report')
def report():
    return render_template('report.html')


@pages_bp.route('/trail/<osm_type>/<int:osm_id>')
def trail_detail(osm_type, osm_id):
    if osm_type not in ('way', 'relation'):
        return 'Invalid trail type', 404
    return render_template('trail.html', osm_type=osm_type, osm_id=osm_id)


@pages_bp.route('/trail/<facility_id>')
def trail_detail_legacy(facility_id):
    return render_template('trail.html', osm_type='legacy', osm_id=facility_id)

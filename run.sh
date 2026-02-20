#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate

# Initialize database
python -c "from database import init_db; init_db()"

# Production: Gunicorn with 4 workers
exec gunicorn app:app \
    --bind 0.0.0.0:8095 \
    --workers 4 \
    --threads 2 \
    --worker-class gthread \
    --timeout 60 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --log-level info

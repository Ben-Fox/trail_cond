import requests

# Shared HTTP session for all external API calls
http_session = requests.Session()
http_session.headers.update({'User-Agent': 'TrailCondish/1.0'})

"""Publish a read-only snapshot through owner-private Sites service access."""
import logging
from urllib.parse import urlparse
import httpx

log = logging.getLogger(__name__)


def publish(settings, html, heartbeat_ts, generated_ts, client=None, report_text=''):
    url = settings.cloud_dashboard_url.rstrip('/')
    token = settings.cloud_dashboard_token.get_secret_value()
    if not url or not token:
        return False
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.chatgpt.site') or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Cloud dashboard must be an HTTPS Sites origin')
    own_client = client is None
    client = client or httpx.Client(timeout=12, follow_redirects=False)
    try:
        response = client.post(url+'/api/snapshot',
            headers={'OAI-Sites-Authorization':'Bearer '+token},
            json={'mode':'paper','html':html,'heartbeat_ts':heartbeat_ts,'generated_ts':generated_ts,'report_text':report_text})
        response.raise_for_status()
        return bool(response.json().get('ok'))
    except (httpx.HTTPError, ValueError):
        # Deliberately do not log request objects, headers or credentials.
        log.warning('Cloud snapshot not published; local agent continues')
        return False
    finally:
        if own_client:
            client.close()

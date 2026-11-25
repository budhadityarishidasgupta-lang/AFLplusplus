"""Permission-gated Brave and provider-scoped SearXNG adapters."""
from dataclasses import dataclass, field as dataclass_field
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from .provider_config import (ConfigurationError, Provider, ProviderConfiguration,
                              build_provider_configuration)

BRAVE_SEARCH_ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
SEARXNG_APPROVED_ENDPOINT = 'https://sovereign-brain-search.onrender.com/search'
MAX_RESULTS = 5
MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class WebSearchConfiguration:
    api_key_configured: bool
    searxng_configured: bool = False
    searxng_endpoint: str | None = dataclass_field(default=None, repr=False)
    configuration_status: str | None = None
    searxng_api_token: str | None = dataclass_field(default=None, repr=False)
    searxng_token_configured: bool = False

    @property
    def provider(self):
        if self.searxng_configured:
            return 'searxng'
        if self.api_key_configured:
            return 'brave'
        return None

    @property
    def configured(self):
        return self.provider is not None


def searxng_configuration(*, supplied=None, environ=None):
    """Build the SearXNG-only view; unrelated environment keys are never read."""
    return build_provider_configuration(Provider.SEARXNG, supplied, environ=environ)


def web_search_configuration(*, environ=None, configuration=None):
    """Compatibility snapshot; new callers should hold ProviderConfiguration.

    Only allowlisted search variables are observed. SearXNG being configured
    never suppresses or triggers Brave implicitly; SearXNG is preferred only
    when explicitly approved and valid. The legacy Brave flag remains visible
    for existing UI compatibility.
    """
    env = os.environ if environ is None else environ
    try:
        gateway = (configuration if configuration is not None
                   else searxng_configuration(environ=env))
        if not isinstance(gateway, ProviderConfiguration) or gateway.provider is not Provider.SEARXNG:
            raise ConfigurationError('SearXNG provider configuration is required.')
        endpoint = gateway.get('endpoint')
        token = gateway._adapter_secret('api_token')
        gateway_configured = gateway.configured and gateway.validated and gateway.capability_enabled
        raw_value = env.get('BRAVE_API_KEY', '')
        raw = raw_value.strip() if isinstance(raw_value, str) else ''
        brave_configured = bool(raw and not raw.upper().startswith(('PASTE_', 'YOUR_', 'REPLACE_')))
        return WebSearchConfiguration(
            api_key_configured=brave_configured,
            searxng_configured=gateway_configured,
            searxng_endpoint=endpoint if gateway_configured else None,
            configuration_status=('not_approved' if gateway.configured and gateway.validated
                                  and not gateway.capability_enabled else None),
            searxng_api_token=token if gateway_configured else None,
            searxng_token_configured=bool(token))
    except ConfigurationError as error:
        # Compatibility status contains stable category only, never supplied values.
        message = str(error)
        status = ('missing_token' if 'token is required' in message else
                  'invalid_endpoint' if 'endpoint' in message else 'invalid_configuration')
        brave_value = env.get('BRAVE_API_KEY', '')
        brave_key = brave_value.strip() if isinstance(brave_value, str) else ''
        brave_configured = bool(brave_key and not brave_key.upper().startswith(
            ('PASTE_', 'YOUR_', 'REPLACE_')))
        return WebSearchConfiguration(brave_configured, configuration_status=status)


def search(query, *, authorized=False, transport=None, max_results=MAX_RESULTS,
           timeout_seconds=DEFAULT_TIMEOUT_SECONDS, configuration=None):
    """Search only after separate per-request authorization.

    `configuration` may be an explicit validated SearXNG ProviderConfiguration
    or the legacy compatibility object. Legacy Brave remains available only for
    existing Controlled flows; Free Learning is routed to configured SearXNG.
    """
    if not authorized:
        return {'status': 'unauthorized', 'results': [], 'error': None}

    config = configuration if configuration is not None else web_search_configuration()
    if not isinstance(config, (ProviderConfiguration, WebSearchConfiguration)):
        return {'status': 'invalid_configuration', 'results': [], 'error': None}
    selected = ('searxng' if isinstance(config, ProviderConfiguration)
                and config.provider is Provider.SEARXNG else getattr(config, 'provider', None))
    if isinstance(config, ProviderConfiguration) and config.provider is not Provider.SEARXNG:
        return {'status': 'invalid_provider_configuration', 'results': [], 'error': None}
    if selected not in ('searxng', 'brave'):
        status = getattr(config, 'configuration_status', None)
        return {'status': status or 'missing_key', 'results': [], 'error': None}

    try:
        limit = min(MAX_RESULTS, max(0, int(max_results)))
    except (TypeError, ValueError, OverflowError):
        limit = MAX_RESULTS
    if not limit:
        return {'status': 'empty', 'results': [], 'error': None}
    if not isinstance(query, str) or not query.strip():
        return {'status': 'invalid_query', 'results': [], 'error': None}
    normalized_query = ' '.join(query.split())[:512]
    if not normalized_query:
        return {'status': 'invalid_query', 'results': [], 'error': None}

    try:
        timeout = min(DEFAULT_TIMEOUT_SECONDS, max(1, float(timeout_seconds)))
    except (TypeError, ValueError, OverflowError):
        timeout = DEFAULT_TIMEOUT_SECONDS

    if isinstance(config, ProviderConfiguration):
        if config.provider is not Provider.SEARXNG:
            return {'status': 'invalid_provider_configuration', 'results': [], 'error': None}
        if not config.configured or not config.validated:
            return {'status': 'invalid_configuration', 'results': [], 'error': None}
        if not config.capability_enabled:
            return {'status': 'not_approved', 'results': [], 'error': None}
        token = config._adapter_secret('api_token')
        endpoint = config.get('endpoint')
        if endpoint != SEARXNG_APPROVED_ENDPOINT or not token:
            return {'status': 'invalid_configuration', 'results': [], 'error': None}
        selected = 'searxng'
    elif selected == 'searxng':
        if getattr(config, 'configuration_status', None) in {
                'not_approved', 'missing_token', 'invalid_endpoint'}:
            status = getattr(config, 'configuration_status')
            return {'status': status, 'results': [], 'error': None}
        endpoint = SEARXNG_APPROVED_ENDPOINT
        configured_endpoint = getattr(config, 'searxng_endpoint', SEARXNG_APPROVED_ENDPOINT)
        token = getattr(config, 'searxng_api_token', None)
        if configured_endpoint != SEARXNG_APPROVED_ENDPOINT:
            return {'status': 'invalid_endpoint', 'results': [], 'error': None}
        if not isinstance(token, str) or not token.strip():
            return {'status': 'missing_token', 'results': [], 'error': None}
        token = token.strip()
    elif selected == 'brave':
        token = None
        endpoint = BRAVE_SEARCH_ENDPOINT
    else:
        return {'status': getattr(config, 'configuration_status', None) or 'missing_key',
                'results': [], 'error': None}

    if selected == 'searxng':
        params = urllib.parse.urlencode({'q': normalized_query, 'format': 'json', 'limit': limit})
        request = urllib.request.Request(
            f'{endpoint}?{params}', headers={'Accept': 'application/json',
                                              'Authorization': f'Bearer {token}'}, method='GET')
    else:
        key_value = os.environ.get('BRAVE_API_KEY', '')
        key = key_value.strip() if isinstance(key_value, str) else ''
        if not key or key.upper().startswith(('PASTE_', 'YOUR_', 'REPLACE_')):
            return {'status': 'missing_key', 'results': [], 'error': None}
        params = urllib.parse.urlencode({'q': normalized_query, 'count': limit})
        request = urllib.request.Request(
            f'{BRAVE_SEARCH_ENDPOINT}?{params}',
            headers={'Accept': 'application/json', 'X-Subscription-Token': key}, method='GET')
    try:
        if callable(transport):
            response = transport(request, timeout)
        else:
            opener = transport.open if transport is not None else urllib.request.urlopen
            response = opener(request, timeout=timeout)
        context = response if hasattr(response, '__enter__') else _ResponseContext(response)
        with context as opened:
            payload_bytes = opened.read(MAX_RESPONSE_BYTES + 1)
        if len(payload_bytes) > MAX_RESPONSE_BYTES:
            return {'status': 'http_error', 'results': [], 'error': 'response_too_large'}
        payload = json.loads(payload_bytes)
    except (TimeoutError, socket.timeout):
        return {'status': 'timeout', 'results': [], 'error': 'timeout'}
    except urllib.error.HTTPError as error:
        try:
            error.close()
        except Exception:
            pass
        return {'status': 'http_error', 'results': [], 'error': 'http_error'}
    except urllib.error.URLError as error:
        if isinstance(getattr(error, 'reason', None), (TimeoutError, socket.timeout)):
            return {'status': 'timeout', 'results': [], 'error': 'timeout'}
        return {'status': 'http_error', 'results': [], 'error': 'http_error'}
    except (OSError, ValueError, TypeError):
        return {'status': 'http_error', 'results': [], 'error': 'http_error'}

    try:
        items = (payload.get('web', {}).get('results', []) if selected == 'brave'
                 else payload.get('results', []))
    except (AttributeError, TypeError):
        return {'status': 'http_error', 'results': [], 'error': 'invalid_response'}
    if not isinstance(items, list):
        return {'status': 'http_error', 'results': [], 'error': 'invalid_response'}
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title, url = item.get('title'), item.get('url')
        snippet = item.get('description') if selected == 'brave' else item.get('content', item.get('snippet'))
        if not all(isinstance(value, str) for value in (title, url, snippet)):
            continue
        if len(url) > 2048:
            continue
        try:
            parsed = urllib.parse.urlsplit(url)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError:
            continue
        if (parsed.scheme.casefold() != 'https' or not hostname or parsed.username is not None
                or parsed.password is not None or any(ord(char) < 32 for char in url)):
            continue
        results.append({'title': title[:500], 'url': url, 'snippet': snippet[:2000],
                        'source': {'provider': selected, 'domain': hostname[:253]}})
        if len(results) >= limit:
            break
    return {'status': 'ok' if results else 'empty', 'results': results, 'error': None}


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc, traceback):
        close = getattr(self.response, 'close', None)
        if close:
            close()
        return False

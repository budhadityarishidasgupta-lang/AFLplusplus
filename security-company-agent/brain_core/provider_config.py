"""Provider-scoped, redacted configuration with explicit capability state.

Precedence: callers pass an explicit mapping; when omitted, each provider may
read only its documented allowlisted environment names. No ambient environment
snapshot is retained. Validation is local-only and never tests connectivity.
"""
from dataclasses import dataclass, field
from enum import Enum
import os
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Redacted, actionable configuration validation failure."""


class Provider(str, Enum):
    SEARXNG = 'searxng'
    OPENAI = 'openai'
    ALIBABA_QWEN = 'alibaba_qwen'
    OLLAMA = 'ollama'


@dataclass(frozen=True)
class ProviderConfiguration:
    provider: Provider
    values: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    configured: bool = False
    validated: bool = False
    availability_verified: bool = False
    capability_enabled: bool = False
    _secret_fields: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __repr__(self):
        fields = {name: ('<redacted>' if name in self._secret_fields else value)
                  for name, value in self.values}
        return (f'ProviderConfiguration(provider={self.provider.value!r}, fields={fields!r}, '
                f'configured={self.configured}, validated={self.validated}, '
                f'availability_verified={self.availability_verified}, '
                f'capability_enabled={self.capability_enabled})')

    def __str__(self):
        return self.__repr__()

    def as_dict(self):
        """Return a redacted diagnostic snapshot; never serialize secrets."""
        return {name: ('<redacted>' if name in self._secret_fields else value)
                for name, value in self.values}

    def get(self, name, default=None):
        """Access an allowlisted value for provider adapters only.

        Secret fields intentionally require the explicit secret accessor to make
        their handling conspicuous at call sites.
        """
        if name in self._secret_fields:
            raise ConfigurationError('Secret values require the provider adapter secret accessor.')
        return dict(self.values).get(name, default)

    def _adapter_secret(self, name):
        """Internal adapter-only secret access; never include the result in logs."""
        if name not in self._secret_fields:
            raise ConfigurationError('Requested field is not a declared provider secret.')
        return dict(self.values).get(name)

    @property
    def secret_field_names(self):
        return self._secret_fields


_SPECS = {
    Provider.SEARXNG: {
        'allowed': frozenset({'endpoint', 'api_token', 'approved'}),
        'secrets': frozenset({'api_token'}),
        'environment': {'endpoint': 'SEARXNG_ENDPOINT', 'api_token': 'SEARXNG_API_TOKEN',
                        'approved': 'SEARXNG_APPROVED'},
    },
    Provider.OPENAI: {
        'allowed': frozenset({'api_key', 'model'}), 'secrets': frozenset({'api_key'}),
        'environment': {'api_key': 'OPENAI_API_KEY', 'model': 'SOVEREIGN_OPENAI_MODEL'},
    },
    Provider.ALIBABA_QWEN: {
        'allowed': frozenset({'api_key', 'region', 'workspace_id', 'endpoint', 'model'}),
        'secrets': frozenset({'api_key'}),
        'environment': {'api_key': 'DASHSCOPE_API_KEY', 'region': 'SOVEREIGN_ALIBABA_REGION',
                        'workspace_id': 'SOVEREIGN_ALIBABA_WORKSPACE_ID',
                        'endpoint': 'SOVEREIGN_ALIBABA_BASE_URL',
                        'model': 'SOVEREIGN_ALIBABA_MODEL'},
    },
    Provider.OLLAMA: {
        'allowed': frozenset({'endpoint', 'model'}), 'secrets': frozenset(),
        'environment': {'endpoint': 'BRAIN_API_BASE', 'model': 'BRAIN_MODEL'},
    },
}


def _provider(provider):
    try:
        return provider if isinstance(provider, Provider) else Provider(provider)
    except (ValueError, TypeError):
        raise ConfigurationError('Unsupported provider. Select a declared provider.') from None


def build_provider_configuration(provider, supplied=None, *, environ=None):
    """Build one provider's configuration, rejecting unknown fields.

    `supplied` is an explicit provider-only mapping and takes precedence over
    the provider's own allowlisted environment variables, including explicit
    blank values. Omitted fields may be read only from that provider's schema.
    Unknown fields fail closed. No environment is mutated and no network I/O
    occurs. Returned token access is intentionally provider-internal.
    """
    provider = _provider(provider)
    spec = _SPECS[provider]
    if supplied is None:
        env = os.environ if environ is None else environ
        raw = {field: env[env_name] for field, env_name in spec['environment'].items()
               if env_name in env}
        # A provider-scoped explicit value mapping is not exposed to other providers.
    else:
        if not hasattr(supplied, 'items'):
            raise ConfigurationError('Provider settings must be a mapping.')
        unknown = set(supplied) - spec['allowed']
        if unknown:
            raise ConfigurationError('Unsupported settings for this provider; remove unrecognized fields.')
        raw = dict(supplied)

    unknown = set(raw) - spec['allowed']
    if unknown:
        raise ConfigurationError('Unsupported settings for this provider; remove unrecognized fields.')
    values = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            raise ConfigurationError(f'{name} must be provided as text.')
        values[name] = value.strip()

    configured, validated, capability_enabled = _validate(provider, values)
    ordered = tuple(sorted(values.items()))
    return ProviderConfiguration(
        provider=provider, values=ordered, configured=configured, validated=validated,
        availability_verified=False, capability_enabled=capability_enabled,
        _secret_fields=spec['secrets'])


def _validate(provider, values):
    """Validate schema locally; never probe a provider or infer authorization."""
    if provider is Provider.SEARXNG:
        endpoint = values.get('endpoint', '')
        token = values.get('api_token', '')
        approval = values.get('approved', '')
        configured = bool(endpoint or token or approval)
        if not configured:
            return False, False, False
        if not endpoint:
            raise ConfigurationError('SearXNG endpoint is required.')
        parsed = urlsplit(endpoint)
        try:
            port = parsed.port
        except ValueError:
            raise ConfigurationError('SearXNG endpoint has an invalid port.') from None
        if (parsed.scheme.casefold() != 'https' or not parsed.hostname or parsed.username
                or parsed.password or port is not None or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') != '/search' or parsed.hostname.casefold() !=
                'sovereign-brain-search.onrender.com'):
            raise ConfigurationError('SearXNG endpoint must be the approved HTTPS search endpoint.')
        if not token:
            raise ConfigurationError('SearXNG authentication token is required.')
        if any(ord(char) < 33 or ord(char) == 127 for char in token):
            raise ConfigurationError('SearXNG authentication token has an invalid format.')
        if approval not in ('', '0', '1'):
            raise ConfigurationError('SearXNG approval flag must be 0 or 1.')
        return True, True, approval == '1'

    if provider is Provider.OPENAI:
        configured = bool(values.get('api_key') or values.get('model'))
        if not configured:
            return False, False, False
        if not values.get('api_key') or not values.get('model'):
            raise ConfigurationError('OpenAI model and API key must both be configured.')
        return True, True, False

    if provider is Provider.ALIBABA_QWEN:
        configured = any(values.values())
        if not configured:
            return False, False, False
        required = ('api_key', 'region', 'workspace_id', 'endpoint', 'model')
        if any(not values.get(name) for name in required):
            raise ConfigurationError('Alibaba Qwen requires its key, region, workspace, HTTPS endpoint, and model.')
        parsed = urlsplit(values['endpoint'])
        try:
            port = parsed.port
        except ValueError:
            raise ConfigurationError('Alibaba Qwen endpoint has an invalid port.') from None
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or port is not None or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') != '/compatible-mode/v1'):
            raise ConfigurationError('Alibaba Qwen endpoint must be an HTTPS compatible-mode endpoint.')
        return True, True, False

    endpoint = values.get('endpoint', 'http://127.0.0.1:11434/v1')
    model = values.get('model', 'qwen3:0.6b')
    parsed = urlsplit(endpoint)
    try:
        parsed.port
    except ValueError:
        raise ConfigurationError('Ollama endpoint has an invalid port.') from None
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.path.rstrip('/') != '/v1' or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not model):
        raise ConfigurationError('Ollama endpoint must be a local HTTP endpoint ending in /v1.')
    values.setdefault('endpoint', endpoint)
    values.setdefault('model', model)
    return True, True, False


def explicit_provider_config(provider, values):
    """Convenience alias emphasizing caller-supplied scoped configuration."""
    return build_provider_configuration(provider, values)


def safe_provider_status(provider, configuration):
    """Return a non-secret diagnostic state for reporting and UI use."""
    provider = _provider(provider)
    if not isinstance(configuration, ProviderConfiguration) or configuration.provider is not provider:
        return {'provider': provider.value, 'configured': False, 'validated': False,
                'availability_verified': False, 'enabled': False, 'status': 'configuration_mismatch'}
    return {'provider': provider.value, 'configured': configuration.configured,
            'validated': configuration.validated,
            'availability_verified': configuration.availability_verified,
            'enabled': configuration.capability_enabled,
            'status': 'ready' if configuration.validated else 'not_configured'}

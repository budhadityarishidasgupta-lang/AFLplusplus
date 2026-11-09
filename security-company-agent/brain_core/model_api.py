"""Replaceable OpenAI-compatible transport; no agent execution or provider logs."""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
import socket
import time
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from .runtime_guard import available_memory_gib
from .model_policy import DEFAULT_LOCAL_MODEL, policy_for

class ModelError(RuntimeError):
    """A safe, user-facing error; excludes response bodies, prompts and credentials."""

class MemoryGuardError(ModelError):
    """Resource limit failure, distinct from structured model-output failure."""

class ModelOutputError(ModelError):
    """Safe validation error with a stable optional diagnostic reference."""
    def __init__(self, message, *, diagnostic_id=None):
        super().__init__(message)
        self.diagnostic_id = diagnostic_id

_REQUEST_LOCK = threading.Lock()

@dataclass(frozen=True)
class ModelConfig:
    base_url: str = 'http://127.0.0.1:11434/v1'
    model: str = DEFAULT_LOCAL_MODEL
    timeout: float = 180
    allow_remote: bool = False
    reasoning_effort: str | None = 'none'
    max_output_tokens: int | None = None
    context_tokens: int | None = None
    keep_alive_seconds: int | None = None

    def __post_init__(self):
        if not isinstance(self.base_url, str):
            raise ModelError('Model endpoint must be an absolute URL.')
        parsed = urlsplit(self.base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelError('Model endpoint must not contain credentials, query strings or fragments.')
        try:
            parsed.port
        except ValueError:
            raise ModelError('Invalid model endpoint port.') from None
        if not parsed.hostname or parsed.path.rstrip('/') != '/v1':
            raise ModelError('Model endpoint must be an absolute URL ending in /v1.')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and self.local):
            raise ModelError('Use HTTPS for enterprise gateways; HTTP is allowed only on loopback.')
        if not self.local and not self.allow_remote:
            raise ModelError('Remote inference requires explicit authorization for session data and references.')
        if not self.model.strip() or not 1 <= self.timeout <= 600:
            raise ModelError('Invalid model name or timeout.')
        for name, value in (('output', self.max_output_tokens), ('context', self.context_tokens)):
            if value is not None and (type(value) is not int or value < 1):
                raise ModelError(f'Invalid {name} token setting.')
        if self.keep_alive_seconds is not None and type(self.keep_alive_seconds) is not int:
            raise ModelError('Invalid keep-alive setting.')

    @property
    def local(self):
        return urlsplit(self.base_url).hostname in ('127.0.0.1', 'localhost', '::1')

    @classmethod
    def from_env(cls, *, allow_remote=False):
        endpoint = os.getenv('BRAIN_API_BASE', 'http://127.0.0.1:11434/v1').rstrip('/')
        effort = os.getenv('BRAIN_REASONING_EFFORT')
        if effort is None:
            effort = 'none' if urlsplit(endpoint).port == 11434 else None
        model = os.getenv('BRAIN_MODEL', DEFAULT_LOCAL_MODEL)
        policy = policy_for(model)
        return cls(endpoint, model, float(os.getenv('BRAIN_TIMEOUT_SECONDS', '180')),
                   allow_remote, effort or None,
                   min(policy.output_tokens, int(os.getenv('BRAIN_MAX_OUTPUT_TOKENS', '0')) or policy.output_tokens),
                   min(policy.context_tokens, int(os.getenv('BRAIN_CONTEXT_TOKENS', '0')) or policy.context_tokens),
                   min(policy.keep_alive_seconds, int(os.getenv('BRAIN_KEEP_ALIVE_SECONDS', '0'))))

@dataclass(frozen=True)
class AlibabaConfiguration:
    """Safe, non-secret Alibaba settings. api_key_configured is boolean only."""
    provider: str
    region: str
    workspace_id: str
    endpoint: str
    model: str
    api_key_configured: bool
    errors: tuple[str, ...]
    timeout_seconds: float = 60
    max_output_tokens: int = 768
    context_tokens: int = 12000

    @property
    def api_key(self):
        """Never retain credentials on a printable configuration object."""
        return os.getenv('DASHSCOPE_API_KEY', '').strip() or None

    @property
    def configured(self):
        return not self.errors

    def __repr__(self):
        return (f'AlibabaConfiguration(provider={self.provider!r}, region={self.region!r}, '
                f'workspace_id={self.workspace_id!r}, endpoint={self.endpoint!r}, model={self.model!r}, '
                f'api_key_configured={self.api_key_configured!r}, errors={self.errors!r}, '
                f'timeout_seconds={self.timeout_seconds!r}, max_output_tokens={self.max_output_tokens!r}, '
                f'context_tokens={self.context_tokens!r})')

    __str__ = __repr__


def alibaba_configuration():
    """Read Alibaba environment configuration without network access or secret exposure."""
    region = os.getenv('SOVEREIGN_ALIBABA_REGION', '').strip()
    workspace = os.getenv('SOVEREIGN_ALIBABA_WORKSPACE_ID', '').strip()
    model = (os.getenv('SOVEREIGN_ALIBABA_MODEL') or os.getenv('ALIBABA_QWEN_MODEL') or '').strip()
    endpoint = (os.getenv('SOVEREIGN_ALIBABA_BASE_URL') or os.getenv('ALIBABA_BASE_URL') or '').strip()
    key = os.getenv('DASHSCOPE_API_KEY', '').strip()
    key_present = bool(key and not key.upper().startswith(('PASTE_', 'YOUR_', 'REPLACE_')))
    errors = []
    region_ids = {
        'cn-beijing': 'cn-beijing', 'beijing': 'cn-beijing',
        'ap-southeast-1': 'ap-southeast-1', 'singapore': 'ap-southeast-1',
        'us-east-1': 'us-east-1', 'us-virginia': 'us-east-1', 'virginia': 'us-east-1',
        'cn-hongkong': 'cn-hongkong', 'hong-kong': 'cn-hongkong', 'hong kong': 'cn-hongkong',
        'ap-northeast-1': 'ap-northeast-1', 'tokyo': 'ap-northeast-1',
        'eu-central-1': 'eu-central-1', 'frankfurt': 'eu-central-1', 'germany-frankfurt': 'eu-central-1',
    }
    if region.upper().startswith(('YOUR_', 'PASTE_', 'REPLACE_')):
        region = ''
    else:
        region = region_ids.get(region.casefold(), region)
    if workspace.upper().startswith(('YOUR_', 'PASTE_', 'REPLACE_')):
        workspace = ''
    if model.upper().startswith(('YOUR_', 'PASTE_', 'REPLACE_')):
        model = ''
    if endpoint.upper().startswith(('YOUR_', 'PASTE_', 'REPLACE_')):
        endpoint = ''
    if not region:
        errors.append('Alibaba region is not configured or is unsupported.')
    elif region not in {'cn-beijing', 'ap-southeast-1', 'us-east-1', 'cn-hongkong', 'ap-northeast-1', 'eu-central-1'}:
        errors.append('Alibaba region is unsupported; configure a documented Model Studio region ID.')
    if (not workspace or len(workspace) > 128
            or any(not (char.isalnum() or char in '-_') for char in workspace)):
        errors.append('Alibaba workspace ID is not configured or invalid.')
    if (not model or len(model) > 120 or any(ord(char) < 32 for char in model)
            or any(char in model for char in ('/', '\\', '?', '#'))):
        errors.append('Alibaba Qwen model is not configured or invalid.')
    if not key_present:
        errors.append('Alibaba API key is not configured.')

    if key_present and any(ord(char) < 32 for char in key):
        key_present = False
        key = ''
        errors.append('Alibaba API key configuration is invalid.')

    # Model Studio endpoints and region-specific keys are not interchangeable.
    # Use the documented workspace-dedicated domain, including Frankfurt.
    endpoint_region = region
    if not endpoint and endpoint_region and workspace:
        endpoint = f'https://{workspace}.{endpoint_region}.maas.aliyuncs.com/compatible-mode/v1'
    if endpoint:
        try:
            parsed = urlsplit(endpoint)
            expected = f'{workspace}.{endpoint_region}.maas.aliyuncs.com'.casefold() if endpoint_region and workspace else None
            valid = (parsed.scheme == 'https' and parsed.username is None and parsed.password is None
                     and parsed.port is None and not parsed.query and not parsed.fragment
                     and parsed.path.rstrip('/') == '/compatible-mode/v1'
                     and bool(parsed.hostname) and parsed.hostname.casefold() == expected)
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            errors.append('Alibaba endpoint must be HTTPS and match the configured workspace and region.')
    try:
        timeout_value = float(os.getenv('SOVEREIGN_ALIBABA_TIMEOUT_SECONDS', '60'))
        if not 1 <= timeout_value <= 180:
            raise ValueError
    except (ValueError, OverflowError):
        timeout_value = 60
        errors.append('Alibaba request timeout must be between 1 and 180 seconds.')
    try:
        max_output = int(os.getenv('SOVEREIGN_ALIBABA_MAX_OUTPUT_TOKENS', '768'))
        if not 1 <= max_output <= 768:
            raise ValueError
    except (ValueError, OverflowError):
        max_output = 768
        errors.append('SOVEREIGN_ALIBABA_MAX_OUTPUT_TOKENS must be between 1 and 768.')
    try:
        context_tokens = int(os.getenv('SOVEREIGN_ALIBABA_CONTEXT_TOKENS', '12000'))
        if not 1 <= context_tokens <= 12000:
            raise ValueError
    except (ValueError, OverflowError):
        context_tokens = 12000
        errors.append('SOVEREIGN_ALIBABA_CONTEXT_TOKENS must be between 1 and 12000.')
    return AlibabaConfiguration('alibaba_qwen_cloud', region, workspace, endpoint, model, key_present,
                                tuple(errors), timeout_value, max_output, context_tokens)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelError('Model endpoint redirects are disabled to protect session data.')

def opener_for(config):
    handlers = [NoRedirect()]
    if config.local:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)

def complete(messages, config, schema):
    with _REQUEST_LOCK:
        return _complete(messages, config, schema)

def _complete(messages, config, schema):
    local_ollama = config.local and urlsplit(config.base_url).port == 11434
    policy = policy_for(config.model)
    if local_ollama:
        memory = available_memory_gib()
        if memory is None:
            raise MemoryGuardError('Local inference is on hold because available physical RAM cannot be verified.')
        if memory < policy.minimum_available_gib:
            raise MemoryGuardError(f'Local inference is on hold: {memory:.2f} GiB RAM is available; '
                                    f'{config.model} requires at least {policy.minimum_available_gib:.1f} GiB available before a request. '
                                    'No generation request was sent.')
        base_endpoint = config.base_url.rsplit('/v1', 1)[0].rstrip('/')
        endpoint = base_endpoint + '/api/chat'
        body = {'model': config.model, 'messages': messages, 'stream': False, 'format': schema,
                'options': {'temperature': 0.3, 'num_ctx': min(policy.context_tokens, config.context_tokens or policy.context_tokens),
                            'num_predict': min(policy.output_tokens, config.max_output_tokens or policy.output_tokens)},
                **({'think': False} if config.model.startswith('qwen3:') else {}),
                'keep_alive': min(policy.keep_alive_seconds, config.keep_alive_seconds if config.keep_alive_seconds is not None else 0)}
    else:
        body = {'model': config.model, 'messages': messages, 'temperature': 0.3,
                'max_tokens': min(policy.output_tokens, config.max_output_tokens or policy.output_tokens), 'stream': False,
                'response_format': {'type': 'json_schema', 'json_schema': {'name': 'emotional_decision', 'strict': True, 'schema': schema}}}
        if config.reasoning_effort:
            body['reasoning_effort'] = config.reasoning_effort
        endpoint = config.base_url.rstrip('/') + '/chat/completions'
    headers = {'Content-Type': 'application/json'}
    if not config.local and os.getenv('BRAIN_API_KEY'):
        headers['Authorization'] = 'Bearer ' + os.environ['BRAIN_API_KEY']
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')
    started = time.perf_counter()
    try:
        with opener_for(config).open(request, timeout=config.timeout) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ModelOutputError('Model response exceeded the response-size limit.')
        payload = json.loads(raw)
        if local_ollama:
            message = payload['message']
            if payload.get('done_reason') == 'length':
                raise ModelOutputError('Model output was truncated. The local model reached its configured output limit.')
            content = message.get('content')
            thinking = message.get('thinking')
            if (not isinstance(content, str) or not content.strip()) and isinstance(thinking, str) and thinking.strip():
                raise ModelOutputError('Model returned reasoning without a structured answer.')
        else:
            choice = payload['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise ModelOutputError('Model output was truncated. Shorten references or increase the output budget.')
            content = choice['message']['content']
        if not isinstance(content, str) or not content.strip():
            raise ModelOutputError('Model returned no structured answer.')
        raw_text = content
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ModelOutputError('Model output must be a JSON object.')
        return result, {'model': config.model, 'seconds': round(time.perf_counter() - started, 3),
                        'source': 'local_model' if config.local else 'authorized_gateway',
                        'provider': 'local_ollama' if local_ollama else 'compatible_gateway',
                        'request_id': payload.get('id'),
                        'input_tokens': payload.get('prompt_eval_count') if local_ollama else payload.get('usage', {}).get('prompt_tokens'),
                        'output_tokens': payload.get('eval_count') if local_ollama else payload.get('usage', {}).get('completion_tokens'),
                        'external_transmission': not config.local, 'raw_text': raw_text}
    except urllib.error.HTTPError as error:
        status = error.code
        diagnostic = error.read(16384).decode('utf-8', errors='replace').casefold()
        error.close()
        if local_ollama and status in (404, 405):
            raise ModelError('Ollama native chat endpoint is unavailable; no compatibility fallback was attempted.') from None
        if status in (401, 403):
            raise ModelError(f'Model service authentication or authorization failed (HTTP {status}).') from None
        if status >= 500 and any(marker in diagnostic for marker in ('out of memory', 'requires more system memory', 'unable to allocate', 'insufficient memory')):
            raise MemoryGuardError('The local model service reported insufficient memory. No automatic retry or replacement model will be attempted.') from None
        raise ModelError(f'Model service returned HTTP {status}. Check model availability and configuration.') from None
    except (TimeoutError, socket.timeout):
        raise ModelError('Model request timed out. Your saved history is unchanged.') from None
    except urllib.error.URLError:
        raise ModelError('Cannot connect to the model service. Check that it is running on the configured endpoint.') from None
    except ModelOutputError:
        raise
    except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
        raise ModelOutputError('Model returned invalid structured output. No training turn was saved.') from None

def local_inventory(timeout=2):
    config = ModelConfig()
    try:
        with opener_for(config).open('http://127.0.0.1:11434/api/tags', timeout=timeout) as response:
            return [item['name'] for item in json.load(response).get('models', [])]
    except (OSError, ValueError, KeyError):
        return []

def check_local_readiness():
    """Inspect Ollama/model/memory readiness without performing inference.

    Returns a stable availability state and an internal diagnostic reason. A
    failed inventory probe is treated as service-offline because it is the
    existing read-only Ollama readiness check.
    """
    from .model_policy import selected_local_model

    model = selected_local_model()
    try:
        inventory = local_inventory()
    except Exception:
        return {'status': 'UNAVAILABLE', 'reason': 'service_offline'}
    if not inventory:
        return {'status': 'UNAVAILABLE', 'reason': 'service_offline'}
    if model not in inventory:
        return {'status': 'UNAVAILABLE', 'reason': 'model_missing'}

    memory = available_memory_gib()
    if memory is not None:
        if memory < policy_for(model).minimum_available_gib:
            return {'status': 'UNAVAILABLE', 'reason': 'insufficient_memory'}
        return {'status': 'AVAILABLE', 'reason': None}
    return {'status': 'AVAILABLE', 'reason': 'unknown'}


def ollama_server_info(timeout=2):
    result = {'version': None, 'loaded_models': None, 'parallel_setting': 'unknown'}
    try:
        opener = opener_for(ModelConfig())
        with opener.open('http://127.0.0.1:11434/api/version', timeout=timeout) as response:
            result['version'] = json.load(response).get('version')
        with opener.open('http://127.0.0.1:11434/api/ps', timeout=timeout) as response:
            result['loaded_models'] = json.load(response).get('models', [])
    except (OSError, ValueError, KeyError):
        return result
    return result

"""Provider-neutral inference contracts; adapters do not own memory or identity."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
import hashlib, hmac, json, socket, ssl, time, urllib.error, urllib.request, urllib.parse

class ProviderID(str, Enum):
    LOCAL_OLLAMA = 'local_ollama'
    ALIBABA_QWEN_CLOUD = 'alibaba_qwen_cloud'
    OPENAI_CLOUD = 'openai_cloud'

@dataclass(frozen=True)
class InferenceRequest:
    messages: tuple[dict[str, str], ...]
    model: str
    max_output_tokens: int = 768
    timeout_seconds: float = 120
    json_schema: dict[str, Any] | None = None

@dataclass(frozen=True)
class InferenceResult:
    provider: ProviderID
    model: str
    text: str
    structured: dict[str, Any] | None
    status: str
    error_category: str | None
    latency_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    request_id: str | None
    estimated_cost: float | None
    cost_is_estimate: bool
    external_transmission: bool

class InferenceProvider(Protocol):
    provider_id: ProviderID
    def infer(self, request: InferenceRequest) -> InferenceResult: ...

def category_for_error(error):
    from .model_api import MemoryGuardError, ModelError, ModelOutputError
    if isinstance(error, MemoryGuardError): return 'resource_limit'
    if isinstance(error, ModelOutputError): return 'invalid_or_unvalidated_output'
    if isinstance(error, (TimeoutError, socket.timeout)): return 'timeout'
    if isinstance(error, ModelError):
        text = str(error).casefold()
        if 'timed out' in text or 'timeout' in text: return 'timeout'
        if 'connect' in text or 'unavailable' in text: return 'connection'
        if 'authentication' in text or 'authorization' in text: return 'authentication'
        return 'provider_error'
    return 'internal_error'

class LocalOllamaProvider:
    provider_id = ProviderID.LOCAL_OLLAMA
    def __init__(self, config): self.config = config
    def infer(self, request):
        from .model_api import complete
        started = time.perf_counter()
        try:
            value, meta = complete(list(request.messages), self.config, request.json_schema)
            return InferenceResult(self.provider_id, request.model, json.dumps(value, ensure_ascii=False), value,
                'success', None, time.perf_counter()-started, meta.get('input_tokens'),
                meta.get('output_tokens'), meta.get('request_id'), None, True, False)
        except Exception as error:
            return InferenceResult(self.provider_id, request.model, '', None, 'failed',
                category_for_error(error), time.perf_counter()-started, None, None, None, None, True, False)

class _ProviderResponseFailure(ValueError):
    def __init__(self, category):
        super().__init__(category)
        self.category = category


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, new_url):
        raise urllib.error.URLError('redirects disabled')


def _alibaba_opener():
    return urllib.request.build_opener(_NoRedirect())


def _schema_accepts(value, schema):
    """Validate JSON Schema constraints without app-only bounded-field assumptions."""
    if not isinstance(schema, dict):
        return False
    kind = schema.get('type')
    if kind == 'object':
        properties = schema.get('properties', {})
        return (isinstance(value, dict) and set(value) == set(properties)
                and all(_schema_accepts(value[key], child) for key, child in properties.items()))
    if kind == 'array':
        return (isinstance(value, list)
                and len(value) >= schema.get('minItems', 0)
                and len(value) <= schema.get('maxItems', len(value))
                and all(_schema_accepts(item, schema.get('items', {})) for item in value))
    if kind == 'string':
        return (isinstance(value, str) and len(value) >= schema.get('minLength', 0)
                and len(value) <= schema.get('maxLength', len(value))
                and (not schema.get('enum') or value in schema['enum']))
    if kind == 'boolean':
        return isinstance(value, bool)
    if kind == 'number':
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value >= schema.get('minimum', value) and value <= schema.get('maximum', value))
    return False


class AlibabaQwenProvider:
    """OpenAI-compatible adapter requiring exact-payload one-time authorization."""
    provider_id = ProviderID.ALIBABA_QWEN_CLOUD
    def __init__(self, *, base_url, model, region, api_key, allowed_hosts,
                 timeout_seconds=60, max_output_tokens=768):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') != '/compatible-mode/v1'):
            raise ValueError('Alibaba endpoint must be the documented HTTPS OpenAI-compatible API base URL.')
        if not parsed.hostname or parsed.hostname.casefold() not in {host.casefold() for host in allowed_hosts}:
            raise ValueError('Alibaba endpoint host does not match configured region/workspace.')
        if (not isinstance(model, str) or not model.strip() or len(model) > 120
                or any(ord(char) < 32 for char in model)
                or any(char in model for char in ('/', '\\', '?', '#'))
                or not isinstance(region, str) or not region.strip()
                or not isinstance(api_key, str) or not api_key.strip()
                or type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 180
                or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 768):
            raise ValueError('Alibaba configuration is incomplete or invalid.')
        self.base_url, self.model, self.region, self._api_key = base_url.rstrip('/'), model, region, api_key
        self.timeout_seconds, self.max_output_tokens = timeout_seconds, max_output_tokens
        self._approved = None
        self._used = set()

    @staticmethod
    def payload(request):
        body = {'model':request.model,'messages':list(request.messages),'stream':False,
                'max_tokens':request.max_output_tokens,'temperature':0.2}
        if request.json_schema:
            schema_name = ('sovereign_general_answer_v1'
                           if request.json_schema.get('properties', {}).get('answer', {}).get('type') == 'string'
                           else 'sovereign_reasoning_v1')
            body['response_format']={'type':'json_schema','json_schema':{
                'name':schema_name,'strict':True,'schema':request.json_schema}}
        return body

    @classmethod
    def fingerprint(cls, request):
        canonical=json.dumps(cls.payload(request),sort_keys=True,ensure_ascii=False,separators=(',',':'))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def authorize_once(self, request, approved_fingerprint):
        fingerprint=self.fingerprint(request)
        if not hmac.compare_digest(fingerprint,approved_fingerprint):
            raise PermissionError('Approval does not match immutable payload.')
        if fingerprint in self._used: raise PermissionError('Approved request already executed.')
        self._approved=fingerprint

    def infer(self, request):
        fingerprint=self.fingerprint(request)
        if fingerprint != self._approved or fingerprint in self._used:
            raise PermissionError('Explicit approval is required for this exact cloud request.')
        # Consume the provider-scoped grant before request construction or network I/O.
        self._approved=None
        self._used.add(fingerprint)
        req=urllib.request.Request(self.base_url+'/chat/completions',
            data=json.dumps(self.payload(request),ensure_ascii=False).encode(),
            headers={'Content-Type':'application/json','Authorization':'Bearer '+self._api_key},method='POST')
        started=time.perf_counter()
        opener = _alibaba_opener()
        try:
            with opener.open(req, timeout=min(self.timeout_seconds, request.timeout_seconds)) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise _ProviderResponseFailure('malformed_response')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise _ProviderResponseFailure('malformed_response')
            choices = payload.get('choices')
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise _ProviderResponseFailure('malformed_response')
            choice = choices[0]
            if choice.get('finish_reason') == 'length':
                raise _ProviderResponseFailure('truncated_output')
            message = choice.get('message')
            text = message.get('content') if isinstance(message, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise _ProviderResponseFailure('empty_response')
            structured = json.loads(text) if request.json_schema else None
            if request.json_schema:
                if not isinstance(structured, dict):
                    raise _ProviderResponseFailure('malformed_structured_output')
                from .openai_provider import _validate_json_schema
                try:
                    _validate_json_schema(structured, request.json_schema)
                except (TypeError, ValueError, KeyError):
                    if not _schema_accepts(structured, request.json_schema):
                        raise _ProviderResponseFailure('malformed_structured_output') from None
            usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
            input_tokens, output_tokens = usage.get('prompt_tokens'), usage.get('completion_tokens')
            if type(input_tokens) is not int or input_tokens < 0:
                input_tokens = None
            if type(output_tokens) is not int or output_tokens < 0:
                output_tokens = None
            request_id = payload.get('id') if isinstance(payload.get('id'), str) else None
            return InferenceResult(self.provider_id, self.model, text, structured, 'success', None,
                time.perf_counter()-started, input_tokens, output_tokens, request_id, None, True, True)
        except _ProviderResponseFailure as error:
            category = error.category
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 401:
                category = 'authentication'
            elif status == 403:
                category = 'account_or_model_access'
            elif status == 404:
                category = 'invalid_model_or_endpoint'
            elif status == 429:
                category = 'rate_limit'
            elif status >= 500:
                category = 'provider_unavailable'
            else:
                category = 'provider_http_error'
        except (TimeoutError, socket.timeout):
            category = 'timeout'
        except urllib.error.URLError:
            category = 'connection'
        except ssl.SSLError:
            category = 'connection'
        except OSError:
            category = 'connection'
        except json.JSONDecodeError:
            category = ('malformed_structured_output' if request.json_schema
                        else 'malformed_response')
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
            category = 'malformed_response'
        except Exception:
            category = 'unexpected_provider_response'
        return InferenceResult(self.provider_id, self.model, '', None, 'failed', category,
            time.perf_counter()-started, None, None, None, None, True, True)

def sanitized_cloud_context(*, scenario, identity_name, identity_mission, approved_lessons=(), recent_turns=(), evidence_bundle=None):
    """Build a narrow cloud context, rejecting secret-bearing or oversized fields."""
    import re

    secret_patterns = (
        re.compile(r'(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|dsk-[A-Za-z0-9_-]{16,})\b'),
        re.compile(r'(?i)\b(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|authorization)\s*[:=]\s*[^\s,;]{4,}'),
        re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}'),
        re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    )

    def safe_text(value, limit, field):
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'Cloud {field} is invalid or exceeds the privacy limit.')
        if any(pattern.search(value) for pattern in secret_patterns):
            raise ValueError(f'Cloud {field} appears to contain a secret and was blocked.')
        return value

    if not isinstance(approved_lessons, (list, tuple)):
        raise ValueError('Cloud approved lessons must be an explicit bounded collection.')
    lessons=[]
    for item in approved_lessons[:3]:
        if isinstance(item, dict) and item.get('approval_status', 'approved') == 'approved':
            selected = {}
            for key, limit in (('lesson_id', 120), ('source_type', 80),
                               ('corrective_principle', 500), ('scenario_summary', 500)):
                value = item.get(key)
                if value is not None:
                    selected[key] = safe_text(value, limit, 'approved lesson')
            lessons.append(selected)
    recent=[]
    # Caller must explicitly select relevant recent context; never pass archive by default.
    for item in recent_turns[-3:]:
        if isinstance(item, dict) and item.get('relevant') is True and item.get('role') in ('user', 'assistant'):
            recent.append({'role': item['role'], 'content': safe_text(item.get('content'), 500, 'recent conversation')})
    context = {'schema_version':'sovereign-context.v1',
        'personality':{'identity_name':safe_text(identity_name, 100, 'identity name'),
                       'mission':safe_text(identity_mission, 500, 'mission guidance')},
        'instructions':['Use reported facts only; separate assumptions and interpretations.',
            'Do not invent evidence or infer hidden motives or feelings.',
            'Approved lessons are prior human-supervised principles, not evidence about this event.',
            'Do not change lesson or persistent emotional state.'],
        'approved_lessons':lessons,'recent_context':recent,'scenario':safe_text(scenario, 8000, 'scenario')}
    if evidence_bundle is not None:
        from .evidence_context import EvidenceContextBundle
        if (not isinstance(evidence_bundle, EvidenceContextBundle)
                or evidence_bundle.excluded_from_system_instructions is not True
                or evidence_bundle.separate_from_approved_memory is not True):
            raise ValueError('Evidence context must be a separated SB-09 bundle.')
        items = list(evidence_bundle.evidence)
        if len(items) > 3:
            raise ValueError('Cloud evidence context exceeds the approved item limit.')
        safe_items = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Cloud evidence item is invalid.')
            title = safe_text(item.get('source_title', item.get('title')), 300, 'external evidence title')
            safe_items.append({
                'citation_id': safe_text(item.get('citation_id', ''), 40, 'external evidence citation'),
                'title': title,
                'source_title': title,
                'source_url': safe_text(item.get('source_url'), 2000, 'external evidence URL'),
                'snippet': safe_text(item.get('snippet'), 1000, 'external evidence snippet'),
                'uncertainty': safe_text(item.get('uncertainty'), 500, 'external evidence uncertainty'),
                'trust': 'untrusted_external_data',
            })
        context['external_evidence'] = {
            'trust': 'untrusted_external_data',
            'notice': evidence_bundle.untrusted_data_notice,
            'items': safe_items,
        }
    return context

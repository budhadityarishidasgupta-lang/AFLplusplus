"""Provider-scoped, memory-only child process launcher.

This is configuration isolation, not an OS sandbox: same-user processes and
application code retain the operating-system access already granted to them.
"""
from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from .provider_config import (ConfigurationError, Provider, ProviderConfiguration,
                              build_provider_configuration)


class RuntimeLaunchError(RuntimeError):
    """Safe launcher error whose message never includes child environment data."""


_WINDOWS_RUNTIME_KEYS = ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP')
_COMMON_RUNTIME_KEYS = ('PATH', 'PYTHONHOME', 'PYTHONUTF8')
_SUPPORTED = frozenset({Provider.SEARXNG})


def read_searxng_configuration(*, input_fn=None, endpoint=None, approved='0'):
    """Prompt for an explicit token without echo; never reads a file or ambient env.

    On Windows, getpass uses masked console input when available; it warns and
    falls back if the console cannot hide input. In that case the operator must
    stop and use a separately approved credential-store integration.
    """
    if input_fn is None:
        input_fn = getpass.getpass
    if not callable(input_fn):
        raise ConfigurationError('Credential input source is unavailable.')
    endpoint = endpoint or 'https://sovereign-brain-search.onrender.com/search'
    try:
        token = input_fn('SearXNG token (input is hidden where supported): ')
    except (EOFError, KeyboardInterrupt):
        raise ConfigurationError('Credential input was cancelled.') from None
    except Exception:
        raise ConfigurationError('Credential input failed.') from None
    if not isinstance(token, str) or not token.strip():
        raise ConfigurationError('SearXNG token is required.')
    return build_provider_configuration(Provider.SEARXNG, {
        'endpoint': endpoint, 'api_token': token, 'approved': approved,
    })


def _fingerprint(configuration):
    canonical = (configuration.provider.value, configuration.values,
                 configuration.configured, configuration.validated,
                 configuration.capability_enabled)
    return hashlib.sha256(repr(canonical).encode('utf-8')).hexdigest()


def build_child_environment(configuration: ProviderConfiguration, *, parent=None,
                             repository_root=None, python_executable=None):
    """Construct a fresh allowlisted environment; never copy arbitrary parent vars."""
    if not isinstance(configuration, ProviderConfiguration):
        raise ConfigurationError('A validated provider configuration is required.')
    if not configuration.configured or not configuration.validated:
        raise ConfigurationError('Provider configuration is missing or invalid.')
    if configuration.provider not in _SUPPORTED:
        raise ConfigurationError('Only the scoped SearXNG Studio runtime is supported.')
    source = os.environ if parent is None else parent
    child = {}
    for key in _COMMON_RUNTIME_KEYS + _WINDOWS_RUNTIME_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value:
            child[key] = value
    child.update({
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONDONTWRITEBYTECODE': '1',
        'SOVEREIGN_LOCAL_MODE': '1',
        'SOVEREIGN_ALLOW_CLOUD_FALLBACK': '0',
        'STREAMLIT_BROWSER_GATHER_USAGE_STATS': 'false',
        'SEARXNG_ENDPOINT': configuration.get('endpoint'),
        'SEARXNG_API_TOKEN': configuration._adapter_secret('api_token'),
        'SEARXNG_APPROVED': configuration.get('approved', '0'),
        'SOVEREIGN_OPENAI_MODEL': '',
        'OPENAI_API_KEY': '',
        'BRAVE_API_KEY': '',
        'SOVEREIGN_ALIBABA_REGION': '',
        'SOVEREIGN_ALIBABA_WORKSPACE_ID': '',
        'SOVEREIGN_ALIBABA_BASE_URL': '',
        'SOVEREIGN_ALIBABA_MODEL': '',
        'ALIBABA_QWEN_MODEL': '',
        'ALIBABA_BASE_URL': '',
        'OPENAI_BASE_URL': '',
        # Local inference remains explicitly loopback-only and uses the
        # previously approved small local model, without cloud provider vars.
        'BRAIN_API_BASE': 'http://127.0.0.1:11434/v1',
        'BRAIN_MODEL': 'qwen3:0.6b',
    })
    root = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[1]
    child['SOVEREIGNBRAIN_RUNTIME_DIR'] = str(root / '.runtime')
    child['SOVEREIGNBRAIN_ISOLATED_RUNTIME'] = '1'
    child['PYTHON_EXECUTABLE'] = str(python_executable or sys.executable)
    child['SOVEREIGN_PROVIDER_CONFIG_FINGERPRINT'] = _fingerprint(configuration)
    return child


def launch_studio(configuration: ProviderConfiguration, *, parent=None,
                  repository_root=None, popen=None, python_executable=None,
                  streamlit_args=None):
    """Launch Studio with an owned process handle; not invoked implicitly."""
    if (not isinstance(configuration, ProviderConfiguration)
            or configuration.provider is not Provider.SEARXNG
            or not configuration.configured or not configuration.validated
            or not configuration.capability_enabled):
        raise ConfigurationError('Validated and explicitly enabled SearXNG configuration is required.')
    env = build_child_environment(configuration, parent=parent,
                                  repository_root=repository_root,
                                  python_executable=python_executable)
    root = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[1]
    command = [str(python_executable or sys.executable), '-m', 'streamlit', 'run',
               str(root / 'brain_studio.py'), '--server.address', '127.0.0.1',
               '--server.headless', 'true', '--server.port', '0',
               '--browser.gatherUsageStats', 'false']
    if streamlit_args:
        raise ConfigurationError('Custom Studio arguments are not permitted in isolated mode.')
    if popen is None:
        popen = subprocess.Popen
    try:
        return popen(command, cwd=str(root), env=env, shell=False,
                      stdin=subprocess.DEVNULL, close_fds=True)
    except Exception:
        env.clear()
        raise RuntimeLaunchError('Studio child process could not be started.') from None


def shutdown_child(process, *, timeout=10):
    """Terminate/wait for only the explicitly owned child process handle."""
    if process is None or not callable(getattr(process, 'poll', None)):
        raise RuntimeLaunchError('Owned child process handle is invalid.')
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout)
        return process.returncode
    except Exception:
        raise RuntimeLaunchError('Owned child process could not be shut down safely.') from None


def run_child(command, configuration, *, parent=None, cwd=None, timeout=10, run=None):
    """Execute a synthetic/helper child with the exact allowlisted environment."""
    if not isinstance(command, (tuple, list)) or not command:
        raise ConfigurationError('A child command is required.')
    env = build_child_environment(configuration, parent=parent)
    if run is None:
        run = subprocess.run
    try:
        return run([str(part) for part in command], cwd=cwd, env=env, shell=False,
                   capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeLaunchError('Child process timed out.') from None
    except Exception:
        raise RuntimeLaunchError('Child process failed safely.') from None
    finally:
        env.clear()

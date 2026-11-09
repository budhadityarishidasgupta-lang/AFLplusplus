# Optional Alibaba Qwen (Model Studio) setup

Cloud inference is **disabled unless a request-specific approval is given in Conversation**. Merely setting a key does not send a request. Do not paste the key into chat, SQLite, source, or issue reports.

## Windows local configuration

Set protected process/user environment variables (PowerShell example; key input is prompted without echo):

```powershell
$secure = Read-Host 'DashScope API key' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $env:DASHSCOPE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
$env:SOVEREIGN_ALIBABA_REGION = 'eu-central-1' # Germany (Frankfurt)
$env:SOVEREIGN_ALIBABA_WORKSPACE_ID = '<your Frankfurt workspace ID>'
$env:SOVEREIGN_ALIBABA_MODEL = '<Qwen model enabled in your Frankfurt workspace>'
# Optional. Otherwise the app constructs the documented Frankfurt workspace URL:
# https://{WorkspaceId}.eu-central-1.maas.aliyuncs.com/compatible-mode/v1
$env:SOVEREIGN_ALIBABA_TIMEOUT_SECONDS = '60'
$env:SOVEREIGN_ALIBABA_MAX_OUTPUT_TOKENS = '768'
$env:SOVEREIGN_ALIBABA_CONTEXT_TOKENS = '12000'
$env:SOVEREIGN_ALIBABA_BASE_URL = ''
```

For persistent Windows user variables, configure them in Windows Environment Variables or a protected secret manager; restart Streamlit after changes. Do not save a plain-text `.env` file unless it has restrictive ACLs and is excluded from Git. For Germany (Frankfurt), the documented workspace-specific OpenAI-compatible base URL is `https://{WorkspaceId}.eu-central-1.maas.aliyuncs.com/compatible-mode/v1`; the app appends `/chat/completions`. Do not use the Singapore `dashscope-intl.aliyuncs.com` endpoint or key for Frankfurt. Region keys, models, and workspace endpoints are not interchangeable. Consult the [official Model Studio base URL reference](https://help.aliyun.com/en/model-studio/base-url) and [Qwen Chat Completions reference](https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions) for other regions. The configured model ID must actually be enabled in your workspace/region; the app does not assume or discover account model availability. Request timeout is configurable from 1 to 180 seconds; output and context budgets are capped at 768 and 12,000 tokens locally.

The app needs no credential display; it reports only whether a key is configured. Model catalog presence is not proof that the workspace key can invoke that model.

## GitHub Actions

In GitHub repository settings, open **Settings → Secrets and variables → Actions → New repository secret**, name it `ALIBABA_DASHSCOPE_API_KEY`, and paste the key into GitHub's secret field. An authorized workflow that genuinely needs cloud access must explicitly map it to `DASHSCOPE_API_KEY` in its job/step environment. Do not expose it to untrusted fork pull requests or print environment variables. Normal CI uses mocked providers and must not call Alibaba. A repository secret is not available to local Streamlit or OpenCode.

## Billing and permissions

Enable Alibaba/provider-side free-quota-only or spending safeguards where available. The app's token/cost budget is an additional guard, not a billing guarantee. Current UI labels prompt token usage as an estimate and does not claim exact cost when no verified price table is configured. Each cloud operation requires approving the exact displayed payload once. No health-check inference is automatic.

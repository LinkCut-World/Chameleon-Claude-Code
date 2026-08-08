# Chameleon Claude Code (ccc)

Chinese: [README_zh.md](README_zh.md)

Windows port of [spideynolove's Claude Code Multi-Provider Setup](https://gist.github.com/spideynolove/13785891385ed6916619ebb991b490b9).

---

One command to switch Claude Code between any Anthropic-compatible provider.

- Manage providers with `ccc providers`
- **Method 1** — `ccc <name>`: start claude with a specific provider
- **Method 2** — `ccc server` + `ccc all`: a local proxy, switch providers in-session with `/model`

## Install

Prerequisites: Claude Code, Python 3.10+.

Method 2 only — run:

```cmd
pip install fastapi "uvicorn[standard]" httpx pydantic
```

Add the `bin` folder to your user PATH.

## Manage providers

```
ccc providers
```

Lists all providers: `A` to add, `D` to delete, or type a number to edit.

A provider's `env` must contain these keys (ccc only reads these five):

- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_DEFAULT_OPUS_MODEL`
- `ANTHROPIC_DEFAULT_SONNET_MODEL`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`

Other keys are ignored by ccc.

When adding a provider, you can enter the values of these 5 keys one by one, or directly
paste the JSON your provider suggests adding to `C:\Users\<username>\.claude\settings.json`
— it already has an `env` key:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://example.com",
    "ANTHROPIC_AUTH_TOKEN": "your API key",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "ANTHROPIC_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES": "thinking,adaptive_thinking,temperature,effort,max_effort",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES": "thinking,adaptive_thinking,temperature,effort,max_effort",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES": "thinking,adaptive_thinking,temperature,effort,max_effort",
    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-5",
    "CLAUDE_CODE_EFFORT_LEVEL": "max"
  },
  "hasCompletedOnboarding": true
}
```

In the pasted text, only those 5 keys are read; everything else is ignored.

## .env (optional)

Optionally create a `.env` file in the repo root with general extra environment
variables; they are injected into every claude launch. For example:

```
CLAUDE_CODE_ATTRIBUTION_HEADER=0
```

## Method 1 — start with a provider

Reads the provider's env vars from the local config and launches claude directly with
them. Start with `ccc <name>`; clear the provider config and go back to default
Anthropic with `ccc clear`.

```cmd
ccc deepseek
ccc kimi -m some-model
```

## Method 2 — switch in-session

Runs a local proxy that routes each request to the provider named in the model field;
claude talks to the proxy, so you switch providers mid-session with `/model`.

Start the proxy in one terminal, then launch claude through it in another:

```cmd
ccc server
ccc all
```

Inside the claude session, the general format is `/model <provider_name>` or
`/model <provider>/<model>`:

```
/model kimi
/model kimi/moonshot-v1-8k
```

- `/model <provider_name>` uses the provider's default opus model, so the provider
  must have `ANTHROPIC_DEFAULT_OPUS_MODEL` set.
- `/model <provider>/<model>` — the model must be one of the provider's configured
  `ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL`.

## Provider requirements

A provider needs an Anthropic-compatible `/v1/messages` API (e.g. DeepSeek, GLM, Kimi).
Providers with only an OpenAI-compatible API need a translation layer, which this tool does not include.

## Security

- The proxy only listens on `127.0.0.1`.
- Your provider data lives in `providers.json` in the repo root; it is gitignored and never uploaded.

#!/usr/bin/env python3
"""
Claude Code Multi-Provider Proxy

A simple local proxy to route Claude Code requests to various LLM providers.

Providers come from `providers.json` (managed by `ccc providers`).
Each provider is `{name, env}` where env carries the 5 ANTHROPIC_* vars.

Routing:
- If the request's `model` field is `provider/model` (e.g. Claude Code's
  `/model myprov/my-model` puts that string in the model field), the
  proxy routes to that provider and rewrites the model to the bare name.
- If the model field is just a provider name (`/model myprov`), it uses that
  provider's default opus model.
- Otherwise it uses the DEFAULT_PROVIDER.

Setup (Windows, via the ccc CLI):
1. Install deps: pip install fastapi "uvicorn[standard]" httpx pydantic
2. Manage providers with: ccc providers
3. Start the proxy with: ccc server   (or: python simple-proxy.py)
4. Connect Claude Code in a NEW terminal with: ccc all
"""
import os
import json
import logging
from typing import Dict, Any, Tuple, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse

# --- Configuration ---
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_PATH = os.path.join(_BASE_DIR, "providers.json")


def _load_providers_from_file() -> Dict[str, Dict[str, Any]]:
    """Read providers.json into {name: {base_url, api_key}}.

    providers.json is the single source of truth. Missing or empty file
    yields an empty dict -> the proxy reports "No providers configured".
    """
    try:
        with open(PROVIDERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    providers = {}
    for p in data.get("providers", []):
        name = p.get("name")
        env = p.get("env", {})
        base_url = env.get("ANTHROPIC_BASE_URL", "")
        if name and base_url:
            providers[name] = {
                "base_url": base_url,
                "api_key": env.get("ANTHROPIC_AUTH_TOKEN", ""),
                # default model names: used only when /model <provider> (no model) selects its opus tier
                "opus_model": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
                "sonnet_model": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
                "haiku_model": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
            }
    return providers


PROVIDERS: Dict[str, Dict[str, Any]] = _load_providers_from_file()
DEFAULT_PROVIDER: str = next(iter(PROVIDERS), "")
DEFAULT_MODEL = ""  # fallback only if the request has no model

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FastAPI Lifespan and App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles application startup and shutdown."""
    logging.info("Starting up proxy server...")
    logging.info(f"Loaded {len(PROVIDERS)} provider(s): {', '.join(PROVIDERS)}")
    yield
    logging.info("Shutting down proxy server...")

app = FastAPI(lifespan=lifespan)

# --- Core Logic ---
def refresh_providers() -> None:
    """Re-read providers.json on each request so tool edits take effect
    without restarting the proxy."""
    global PROVIDERS, DEFAULT_PROVIDER
    PROVIDERS = _load_providers_from_file()
    DEFAULT_PROVIDER = next(iter(PROVIDERS), "")


def messages_url(base_url: str) -> str:
    """Return the /v1/messages endpoint for a provider's ANTHROPIC_BASE_URL."""
    if base_url.endswith("/v1/messages"):
        return base_url
    return base_url.rstrip("/") + "/v1/messages"


def get_provider_config(provider_key: str) -> Dict[str, Any]:
    """Retrieves provider configuration and API key."""
    config = PROVIDERS.get(provider_key)
    if not config:
        raise HTTPException(status_code=400, detail=f"Invalid provider specified: {provider_key}")

    api_key = config.get("api_key")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail=f"API key (ANTHROPIC_AUTH_TOKEN) not set for provider '{provider_key}'. "
                   f"Set it via ccc providers.",
        )
    return {"base_url": messages_url(config["base_url"]), "api_key": api_key}


def parse_model_field(model: str) -> Optional[Tuple[str, str]]:
    """If the model field is 'provider/model' and provider is known, split it."""
    if model and "/" in model:
        provider, name = model.split("/", 1)
        if provider in PROVIDERS:
            return provider, name
    return None


def parse_model_command(body: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """
    Legacy: parses a `/model <provider>/<model_name>` command from the user's
    messages. Modern Claude Code consumes `/model` client-side (it lands in the
    model field), so this is only a fallback.
    """
    messages = body.get("messages", [])
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if msg.get("role") == "user" and isinstance(content, str) and content.strip().startswith("/model "):
            parts = content.strip().split()
            if len(parts) == 2:
                model_identifier = parts[1]
                if "/" in model_identifier:
                    cleaned_content = content.replace(f"/model {model_identifier}", "").strip()
                    if cleaned_content:
                        messages[i]["content"] = cleaned_content
                    else:
                        messages.pop(i)
                    return model_identifier.split("/", 1)
    return None


# --- API Endpoints ---
@app.post("/v1/messages")
async def messages_proxy(request: Request):
    """
    Main endpoint: determine the target provider from the model field (or a
    legacy /model command in the messages), then forward the request.
    """
    refresh_providers()

    if not PROVIDERS:
        raise HTTPException(
            status_code=500,
            detail="No providers configured. Run ccc providers and add a provider.",
        )

    body = await request.json()
    provider_key = DEFAULT_PROVIDER
    model_name = (body.get("model") or "").strip() or DEFAULT_MODEL

    # Method A: Claude Code's /model provider/model lands in the model field; route by it
    parsed = parse_model_field(model_name)
    if parsed:
        provider_key, model_name = parsed
        logging.info(f"Routing via model field: {provider_key}/{model_name}")
    elif model_name in PROVIDERS:
        # Special case: the model field is exactly a provider name (e.g. /model <provider>) ->
        # use that provider's default opus model (fall back to sonnet/haiku)
        cfg = PROVIDERS[model_name]
        default_model = (
            cfg.get("opus_model") or cfg.get("sonnet_model") or cfg.get("haiku_model")
        )
        if not default_model:
            raise HTTPException(
                status_code=400,
                detail=f"Provider '{model_name}' has no default model "
                       f"(ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL) in providers.json.",
            )
        provider_key = model_name
        model_name = default_model
        logging.info(f"Routing via provider-only model: {provider_key} -> {model_name}")
    else:
        # Legacy: scan messages for a "/model ..." command, as a fallback
        model_command = parse_model_command(body)
        if model_command:
            provider_key, model_name = model_command
            logging.info(f"Routing via /model command: {provider_key}/{model_name}")

    if not model_name:
        raise HTTPException(
            status_code=400,
            detail="No model specified in the request. Type /model <provider>/<model> to pick one.",
        )

    body["model"] = model_name

    try:
        config = get_provider_config(provider_key)
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(300.0)  # 5-minute timeout for responses
        async with httpx.AsyncClient(timeout=timeout) as client:
            logging.info(f"Routing request to {provider_key} at {config['base_url']} with model {model_name}")

            response = await client.post(
                url=config["base_url"],
                json=body,
                headers=headers,
            )
            response.raise_for_status()

            # Handle streaming vs. non-streaming responses
            if body.get("stream", False):
                async def stream_generator():
                    async for chunk in response.aiter_bytes():
                        yield chunk
                return StreamingResponse(stream_generator(), media_type=response.headers.get("content-type"))
            else:
                return response.json()

    except httpx.HTTPStatusError as e:
        logging.error(f"HTTP Error from provider {provider_key}: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """A simple health check endpoint."""
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    """Model catalog so Claude Code can validate /model names.

    Claude Code calls GET /v1/models when validating a model (e.g. /model krill).
    Without it the proxy returns 404 and validation fails. We list, per provider:
    the provider name itself, its opus/sonnet/haiku defaults, and provider/model combos.
    """
    refresh_providers()
    data = []
    for name, cfg in PROVIDERS.items():
        defaults = [m for m in (cfg.get("opus_model"), cfg.get("sonnet_model"), cfg.get("haiku_model")) if m]
        # Only list "provider name" and "provider/model". Bare model names are not listed —
        # a bare name (e.g. krill-opus) has no provider prefix, so the proxy cannot route it
        # back to krill; listing it would make it look usable while it would fall to the default provider.
        ids = [name] + [f"{name}/{m}" for m in defaults]
        for mid in ids:
            data.append({"id": mid, "object": "model", "created": 0, "owned_by": name})
    return {"data": data}


# --- Main Execution ---
if __name__ == "__main__":
    import uvicorn
    # Runs on 127.0.0.1 (localhost) for better security.
    uvicorn.run(app, host="127.0.0.1", port=8787)

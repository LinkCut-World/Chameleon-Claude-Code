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
- Model names may carry a Claude Code context-window suffix (`[1m]`). It is not
  part of the model id, so the proxy strips it — both from the names it reads
  out of providers.json and from incoming requests — before matching or
  forwarding upstream.
- Anything else is refused with a 502. The proxy never picks a provider on
  your behalf, so a model you did not ask for can never be billed or sent
  your conversation. Launch with `ccc all <provider>` to pin Claude Code's
  own background/subagent calls to one provider.

Setup (Windows, via the ccc CLI):
1. Install deps: pip install fastapi "uvicorn[standard]" httpx pydantic
2. Manage providers with: ccc providers
3. Start the proxy with: ccc server   (or: python simple-proxy.py)
4. Connect Claude Code in a NEW terminal with: ccc all
"""
import os
import re
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
    global PROVIDERS
    PROVIDERS = _load_providers_from_file()


def messages_url(base_url: str) -> str:
    """Return the /v1/messages endpoint for a provider's ANTHROPIC_BASE_URL."""
    if base_url.endswith("/v1/messages"):
        return base_url
    return base_url.rstrip("/") + "/v1/messages"


# A trailing "[...]" on a model name is a Claude Code context-window hint, not
# part of the model id: "[1m]" tells it to assume a 1M window. providers.json
# stores tier names exactly as configured, suffix and all, because Method 1
# (`ccc <provider>`) passes them to Claude Code verbatim and the hint must
# survive. The proxy is the other side of that: it matches and forwards bare
# ids, so it strips the suffix off anything it reads from providers.json or
# receives in a request.
CONTEXT_SUFFIX_RE = re.compile(r"\[[^\[\]]*\]$")


def strip_context_suffix(name: str) -> str:
    """'deepseek-v4-flash[1m]' -> 'deepseek-v4-flash'. The bare id, safe upstream."""
    return CONTEXT_SUFFIX_RE.sub("", name)


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


def upstream_error(provider_key: str, status: int, detail: str) -> HTTPException:
    """Translate an upstream failure into one Claude Code reports usefully.

    Claude Code reads 401/403 as "you are not signed in" and tells the user to
    run /login — advice that is wrong here, because the proxy holds the real
    credentials and the dummy token the client sends is never checked. Relaying
    the upstream 401 verbatim is what makes a working session suddenly demand a
    login. Report it as 502 (an upstream problem, which it is) and put the
    provider and original status in the message.
    """
    logging.error(f"Upstream {provider_key} returned {status}: {detail[:500]}")
    if status in (401, 403):
        return HTTPException(
            status_code=502,
            detail=f"Provider '{provider_key}' rejected the request with {status} "
                   f"(bad/expired ANTHROPIC_AUTH_TOKEN, or no access to this model). "
                   f"This is not a Claude Code login problem — fix the key with "
                   f"`ccc providers`. Upstream said: {detail[:500]}",
        )
    return HTTPException(status_code=status, detail=f"Provider '{provider_key}': {detail[:1000]}")


def parse_model_field(model: str) -> Optional[Tuple[str, str]]:
    """If the model field is 'provider/model' and provider is known, split it.

    The model half may carry a context-window suffix; strip it so only the bare
    id goes upstream.
    """
    if model and "/" in model:
        provider, name = model.split("/", 1)
        if provider in PROVIDERS:
            return provider, strip_context_suffix(name)
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


def refuse_unprefixed(model: str) -> HTTPException:
    """Refuse a model name that does not name a provider.

    The proxy never guesses. A name with no provider prefix cannot be attributed
    to anyone, and picking a provider on the user's behalf would silently bill
    and expose the conversation to a model they did not choose. Refuse instead,
    and say how to make it routable.

    Reported as 502, not 401/403: Claude Code renders those two as
    "Not logged in - please run /login", which would be a wrong diagnosis.
    """
    known = ", ".join(sorted(PROVIDERS)) or "(none configured)"
    return HTTPException(
        status_code=502,
        detail=f"Model '{model}' does not name a configured provider, so the proxy "
               f"will not route it (it never picks a provider for you). "
               f"Use /model <provider> or /model <provider>/<model>. "
               f"Known providers: {known}. If Claude Code sent this on its own "
               f"(subagent or background call), launch with `ccc all <provider>` "
               f"so the model tiers are pinned to that provider.",
    )


# --- API Endpoints ---
@app.post("/v1/messages/count_tokens")
async def count_tokens_proxy(request: Request):
    """Token-counting endpoint.

    Claude Code calls this to size the context. Without it the proxy 404s,
    which shows up as spurious context/usage errors mid-session.
    """
    refresh_providers()
    if not PROVIDERS:
        raise HTTPException(status_code=500, detail="No providers configured.")
    body = await request.json()
    provider_key, model_name = route_request(body)
    body["model"] = model_name
    config = get_provider_config(provider_key)
    url = messages_url(config["base_url"]) + "/count_tokens"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            r = await client.post(url, json=body, headers=auth_headers(config["api_key"]))
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logging.warning(f"count_tokens upstream failed ({e}); using local estimate.")
    # Providers need not implement count_tokens. Estimate rather than fail:
    # a 404 here would surface to the user as a broken session.
    approx = len(json.dumps(body.get("messages", []), ensure_ascii=False)) // 4
    return {"input_tokens": approx}


def route_request(body: Dict[str, Any]) -> Tuple[str, str]:
    """Decide which provider handles this request and under which model name.

    Every route must be stated explicitly by the model field. There is no
    default-provider fallback: an unattributable name is refused, never guessed.
    """
    model_name = (body.get("model") or "").strip()

    # Method A: Claude Code's /model provider/model lands in the model field; route by it
    parsed = parse_model_field(model_name)
    if parsed:
        logging.info(f"Routing via model field: {parsed[0]}/{parsed[1]}")
        return parsed
    # The model field is exactly a provider name -> use that provider's default
    # opus model (falling back to sonnet/haiku). Tolerate a context suffix here:
    # Claude Code normally strips it before sending, but a request that arrives
    # with one still names this provider and should route, not be refused.
    provider_only = strip_context_suffix(model_name)
    if provider_only in PROVIDERS:
        cfg = PROVIDERS[provider_only]
        default_model = (
            cfg.get("opus_model") or cfg.get("sonnet_model") or cfg.get("haiku_model")
        )
        if not default_model:
            raise HTTPException(
                status_code=400,
                detail=f"Provider '{provider_only}' has no default model "
                       f"(ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL) in providers.json.",
            )
        # providers.json stores the name as configured, suffix and all; strip it.
        default_model = strip_context_suffix(default_model)
        logging.info(f"Routing via provider-only model: {model_name} -> {default_model}")
        return provider_only, default_model

    # Legacy: scan messages for a "/model ..." command, as a fallback
    model_command = parse_model_command(body)
    if model_command:
        logging.info(f"Routing via /model command: {model_command[0]}/{model_command[1]}")
        return model_command

    # Unprefixed name (Claude Code's own background/subagent calls, or a stale
    # saved model): refuse rather than pick a provider on the user's behalf.
    if not model_name:
        raise HTTPException(
            status_code=400,
            detail="No model specified in the request. Type /model <provider>/<model> to pick one.",
        )
    logging.warning(f"Refusing unroutable model '{model_name}' (no provider prefix)")
    raise refuse_unprefixed(model_name)


def auth_headers(api_key: str) -> Dict[str, str]:
    """Send the key both ways: providers differ on which header they accept."""
    return {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }


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
    provider_key, model_name = route_request(body)
    body["model"] = model_name

    config = get_provider_config(provider_key)
    headers = auth_headers(config["api_key"])
    timeout = httpx.Timeout(600.0, connect=30.0)
    logging.info(f"Routing request to {provider_key} at {config['base_url']} with model {model_name}")

    if body.get("stream", False):
        # Keep the client open for the life of the stream: closing the context
        # manager before the body is consumed truncates long responses.
        client = httpx.AsyncClient(timeout=timeout)
        try:
            req = client.build_request("POST", config["base_url"], json=body, headers=headers)
            response = await client.send(req, stream=True)
        except Exception as e:
            await client.aclose()
            logging.error(f"Upstream connection error for {provider_key}: {e}")
            raise HTTPException(status_code=502, detail=f"Upstream {provider_key} unreachable: {e}")
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", "replace")
            await response.aclose()
            await client.aclose()
            raise upstream_error(provider_key, response.status_code, detail)

        async def stream_generator():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "text/event-stream"),
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url=config["base_url"], json=body, headers=headers)
    except Exception as e:
        logging.error(f"Upstream connection error for {provider_key}: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream {provider_key} unreachable: {e}")
    if response.status_code >= 400:
        raise upstream_error(provider_key, response.status_code, response.text)
    return response.json()


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
        # providers.json stores names as configured, so a tier may carry a
        # context suffix ("deepseek-v4-flash[1m]"). Catalog entries name what the
        # proxy routes on, and routing uses the bare id — Claude Code strips the
        # suffix before sending, so a suffixed entry would never be matched.
        defaults = [
            strip_context_suffix(m)
            for m in (cfg.get("opus_model"), cfg.get("sonnet_model"), cfg.get("haiku_model"))
            if m
        ]
        # Only list "provider name" and "provider/model". Bare model names are not listed —
        # a bare name (e.g. krill-opus) has no provider prefix, so the proxy cannot route it
        # back to krill; listing it would make it look usable while it would fall to the default provider.
        # dict.fromkeys dedups while keeping order: the tiers often share one model name.
        ids = [name] + [f"{name}/{m}" for m in dict.fromkeys(defaults)]
        for mid in ids:
            data.append({"id": mid, "object": "model", "created": 0, "owned_by": name})
    return {"data": data}


# --- Main Execution ---
if __name__ == "__main__":
    import uvicorn
    # Runs on 127.0.0.1 (localhost) for better security.
    uvicorn.run(app, host="127.0.0.1", port=8787)

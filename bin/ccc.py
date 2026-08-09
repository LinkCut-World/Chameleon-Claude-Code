#!/usr/bin/env python3
"""ccc - Chameleon Claude Code

A single CLI to manage Claude Code's multi-provider configuration.

Usage:
  ccc                         Show help
  ccc providers               Manage providers (add / edit / delete)
  ccc <provider> [claude args]  Start claude with a provider
  ccc clear [claude args]     Clear provider config, use default Anthropic
  ccc server                  Start the local proxy (Method 2 server)
  ccc all <provider> [claude args]  Start claude through the proxy on a provider
"""
import sys
import os
import json
import re
import subprocess
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # ...\bin
BASE = os.path.dirname(SCRIPT_DIR)                        # project root
PROVIDERS_FILE = os.path.join(BASE, "providers.json")
ENV_FILE = os.path.join(BASE, ".env")
PROXY_BASE = "http://127.0.0.1:8787"

ENV_FIELDS = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
]

# Provider vars to clear for the claude child process (isolated subprocess).
ANTHROPIC_VARS = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
]

# Reserved subcommand names; providers cannot use these names.
RESERVED = {"providers", "clear", "server", "all", "help", "-h", "--help"}


# ---------- Basics ----------
def ensure_utf8():
    if os.name == "nt":
        os.system("chcp 65001 >nul")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def load_data():
    if os.path.exists(PROVIDERS_FILE):
        try:
            with open(PROVIDERS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"[WARN] Failed to parse {PROVIDERS_FILE}, treating it as empty.")
            return {}
    return {}


def save_data(data):
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_providers(data):
    return data.setdefault("providers", [])


def find_provider(data, name):
    for p in get_providers(data):
        if p["name"] == name:
            return p
    return None


def is_valid_name(name):
    return bool(name) and re.fullmatch(r"[A-Za-z0-9_-]+", name)


def apply_dotenv():
    """Inject the project .env generic vars into this process env (if present)."""
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


def claude_settings_path():
    """Path to Claude Code's user settings.json (honours CLAUDE_CONFIG_DIR)."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return os.path.join(cfg, "settings.json")


def saved_model():
    """The top-level "model" Claude Code persisted from the last /model pick.

    Claude Code writes this whenever you switch model in-session ("Set model to
    X and saved as your default for new sessions"), and it outranks the
    ANTHROPIC_DEFAULT_*_MODEL env vars on the next launch.
    """
    try:
        with open(claude_settings_path(), encoding="utf-8") as f:
            return (json.load(f).get("model") or "").strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def is_anthropic_model(model):
    """True for names api.anthropic.com understands (aliases and claude-* ids)."""
    m = model.strip().lower()
    return m in {"opus", "sonnet", "haiku", "opusplan", "default"} or m.startswith("claude-")


def has_model_arg(args):
    """True if the user already passed --model / -m themselves."""
    for a in args:
        if a == "--model" or a == "-m" or a.startswith("--model="):
            return True
    return False


def launch_claude(args, model=None):
    """Run claude as a subprocess, inheriting this process's environment.

    Resolve claude with shutil.which (on Windows it is a .cmd installed by npm;
    subprocess.run(["claude"]) alone cannot find it, so resolve the full path).

    `model` is passed through as --model. This matters because Claude Code
    persists the model you pick with /model into ~/.claude/settings.json as a
    top-level "model" key, and that saved value outranks the
    ANTHROPIC_DEFAULT_*_MODEL env vars we set here. Without an explicit
    --model, a model saved by an earlier session (possibly for a different
    provider) leaks into every later launch. --model overrides the saved
    setting for this session only and does not write it back.
    """
    import shutil
    exe = shutil.which("claude")
    if not exe:
        print("[ERROR] claude command not found. Install it first: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)
    argv = list(args)
    if model and not has_model_arg(argv):
        argv = ["--model", model] + argv
    return subprocess.run([exe] + argv)


def server_up():
    try:
        urllib.request.urlopen(PROXY_BASE + "/health", timeout=2)
        return True
    except Exception:
        return False


# ---------- Subcommands ----------
def cmd_help():
    print("""ccc - Chameleon Claude Code

Usage:
  ccc providers               Manage providers (add / edit / delete)
  ccc <provider> [claude args]  Start claude with a provider
  ccc clear [claude args]     Clear provider config, use default Anthropic
  ccc server                  Start the local proxy (Method 2 server, keep running)
  ccc all <provider> [claude args]
                              Start claude through the proxy on <provider>
  ccc help                    Show this help

Examples:
  ccc providers               # open the provider manager
  ccc my-provider --model sonnet   # start claude with a provider
  ccc server                  # start the proxy in terminal A
  ccc all my-provider         # start claude through the proxy in terminal B
""", end="")


def provider_tier_alias(env):
    """Pick the --model alias matching the tiers this provider actually defines.

    Claude Code resolves the aliases opus/sonnet/haiku through
    ANTHROPIC_DEFAULT_<TIER>_MODEL, so the alias yields this provider's own
    model name. Skip tiers the provider left blank: for those the alias falls
    back to a built-in Anthropic model name the provider likely rejects.
    """
    for alias, field in (
        ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
        ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
        ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ):
        if env.get(field, "").strip():
            return alias
    return None


def cmd_run(name, args):
    data = load_data()
    p = find_provider(data, name)
    if not p:
        print(f"[ERROR] Provider '{name}' does not exist. Run ccc providers to add it.")
        sys.exit(1)
    env = p.get("env", {})
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        print(f"[ERROR] Provider '{name}' has an empty ANTHROPIC_AUTH_TOKEN. Run ccc providers to edit it.")
        sys.exit(1)
    apply_dotenv()
    # Clear provider vars first, then set only this provider's — so leftover
    # ANTHROPIC_API_KEY/AUTH_TOKEN from the environment cannot coexist and
    # trigger Claude Code's auth-conflict warning.
    for k in ANTHROPIC_VARS:
        os.environ.pop(k, None)
    os.environ.update(env)
    alias = provider_tier_alias(env)
    if alias is None:
        print(f"[WARN] Provider '{name}' defines no default model; "
              f"claude may start on whatever model it last saved.")
    print(f"Starting claude with {name} ...")
    launch_claude(args, model=alias)


def cmd_clear(args):
    apply_dotenv()
    for k in ANTHROPIC_VARS:
        os.environ.pop(k, None)
    # A provider-specific model saved by an earlier ccc session (e.g. "deepseek")
    # would otherwise be sent to api.anthropic.com, which does not know it.
    # Only override when the saved value is clearly not an Anthropic model.
    model = None
    saved = saved_model()
    if saved and not is_anthropic_model(saved):
        model = "opus"
        print(f"Saved model '{saved}' is not an Anthropic model; starting on opus instead.")
    print("Cleared provider config, starting claude with default Anthropic ...")
    launch_claude(args, model=model)


def cmd_server():
    apply_dotenv()
    proxy = os.path.join(BASE, "simple-proxy.py")
    print(f"Starting local proxy ... serving at {PROXY_BASE} (Ctrl+C to stop)")
    sys.exit(subprocess.run([sys.executable, proxy]).returncode)


def cmd_all(name, args):
    data = load_data()
    providers = get_providers(data)
    if not providers:
        print("[ERROR] No providers configured yet. Run ccc providers to add one.")
        sys.exit(1)
    if not name:
        print("[ERROR] ccc all needs a provider: ccc all <provider> [claude args]")
        print("        Configured: " + ", ".join(p["name"] for p in providers))
        sys.exit(1)
    p = find_provider(data, name)
    if not p:
        print(f"[ERROR] Provider '{name}' does not exist. Run ccc providers to add it.")
        sys.exit(1)
    env = p.get("env", {})
    if not server_up():
        print(f"[ERROR] Local proxy is not running ({PROXY_BASE}/health unreachable).")
        print("        Start it in another terminal first: ccc server")
        sys.exit(1)
    apply_dotenv()
    for k in ANTHROPIC_VARS:
        os.environ.pop(k, None)
    # Use ANTHROPIC_AUTH_TOKEN instead of ANTHROPIC_API_KEY: claude's /model
    # validation resolves auth reliably with AUTH_TOKEN; with API_KEY it can
    # report "Could not resolve authentication method". The proxy does the real
    # auth, so the value here is just a dummy.
    os.environ["ANTHROPIC_BASE_URL"] = PROXY_BASE
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "dummy"

    # Point every tier at "<provider>/<model>". Claude Code resolves its own
    # background and subagent calls through these vars, so prefixing them keeps
    # those requests routable: without a prefix it sends built-in names like
    # claude-opus-5, which name no provider and the proxy refuses.
    tiers = {}
    for var in ("ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        model = env.get(var, "").strip()
        if model:
            tiers[var] = f"{name}/{model}"
    if not tiers:
        print(f"[ERROR] Provider '{name}' defines no default model "
              f"(ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL). Run ccc providers to set one.")
        sys.exit(1)
    os.environ.update(tiers)
    # Tiers the provider left blank would otherwise fall back to a built-in
    # Anthropic name, so start on a tier it actually defines.
    alias = provider_tier_alias(env)

    print(f"Starting claude through the local proxy on {name} ... "
          f"switch providers in-session with /model provider/model")
    launch_claude(args, model=alias)


# ---------- Provider manager (interactive) ----------
def input_env_from_json():
    print("Paste the JSON (may span multiple lines); the env key holds the 5 fields. "
          "Finish with a line containing just END:")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    try:
        obj = json.loads("\n".join(lines))
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON: {e}")
        return None
    env = obj.get("env") if isinstance(obj, dict) else None
    if not isinstance(env, dict):
        print('JSON is missing an env object (expected {"env": {...5 fields...}})')
        return None
    result = {}
    for field in ENV_FIELDS:
        result[field] = str(env.get(field, "")).strip()
    return result


def input_env_wizard_or_json():
    while True:
        print("How to enter the remaining 5 fields:")
        print("  [1] Enter one by one")
        print("  [2] Paste JSON (env key with the 5 fields)")
        choice = input("> ").strip()
        if choice == "2":
            env = input_env_from_json()
            if env is not None:
                return env
            continue
        if choice == "1":
            env = {}
            for field in ENV_FIELDS:
                val = input(f"{field}: ").strip()
                env[field] = val
            return env
        print("Invalid input, choose 1 or 2.")


def prompt_name(new=False):
    label = "New provider name" if new else "New name"
    while True:
        name = input(f"{label} (letters/numbers/_/-, blank to cancel): ").strip()
        if not name:
            return None
        if not is_valid_name(name):
            print("Name is invalid; only letters/numbers/_/- allowed.")
            continue
        return name


def add_provider(data):
    providers = get_providers(data)
    name = prompt_name(new=True)
    if name is None:
        print("Cancelled.")
        return
    if name in RESERVED:
        print(f"Name '{name}' is a reserved subcommand and cannot be used as a provider name.")
        return
    if find_provider(data, name):
        print(f"Name '{name}' already exists; duplicates are not allowed.")
        return
    env = input_env_wizard_or_json()
    providers.append({"name": name, "env": env})
    save_data(data)
    print(f"Added provider '{name}'. You can now run: ccc {name}")


def delete_provider(data, idx):
    providers = get_providers(data)
    name = providers[idx]["name"]
    confirm = input(f"Delete provider '{name}'? (y/N): ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return
    providers.pop(idx)
    save_data(data)
    print(f"Deleted '{name}'.")


def edit_provider(data, idx):
    providers = get_providers(data)
    p = providers[idx]
    name = p["name"]
    env = p.get("env", {})

    while True:
        print(f"\n=== Editing provider: {name} ===")
        print("  [1] name: %s" % name)
        for i, field in enumerate(ENV_FIELDS, 2):
            print(f"  [{i}] {field}: {env.get(field, '')}")
        print("  [S] Save and return")
        print("  [X] Discard and return")
        choice = input("> ").strip().lower()

        if choice == "s":
            save_data(data)
            print(f"Saved '{name}'.")
            return
        if choice == "x":
            print("Discarded changes.")
            return
        if choice.isdigit():
            n = int(choice)
            if n == 1:
                new_name = prompt_name()
                if new_name is None:
                    continue
                if new_name in RESERVED:
                    print(f"Name '{new_name}' is a reserved subcommand and cannot be used as a provider name.")
                    continue
                if find_provider(data, new_name) and new_name != name:
                    print(f"Name '{new_name}' collides with another provider; this change is not allowed.")
                    continue
                name = new_name
                p["name"] = new_name
                print(f"Name changed to '{new_name}'.")
            elif 2 <= n <= len(ENV_FIELDS) + 1:
                field = ENV_FIELDS[n - 2]
                cur = env.get(field, "")
                new_val = input(f"{field} (current: {cur}, blank to keep, - to clear): ")
                if new_val == "":
                    continue
                if new_val.strip() == "-":
                    env[field] = ""
                    print(f"{field} cleared.")
                else:
                    env[field] = new_val
                    print(f"{field} updated.")
            else:
                print("Invalid number.")
        else:
            print("Invalid input.")


def cmd_providers():
    data = load_data()
    data.setdefault("providers", [])

    while True:
        providers = get_providers(data)
        print("\n=== Chameleon Claude Code - Provider Manager ===")
        if not providers:
            print("  (no providers yet — add one and the matching `ccc <name>` becomes available)")
        else:
            for i, p in enumerate(providers, 1):
                print(f"  [{i}] {p['name']}")
        print()
        print("  [A] Add provider")
        print("  [D] Delete provider")
        print("  [Q] Back")
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            break

        if choice in ("q", "quit", "exit", "back"):
            break
        if choice == "a":
            add_provider(data)
        elif choice == "d":
            if not providers:
                print("Nothing to delete.")
                continue
            idx = input("Enter number to delete: ").strip()
            if idx.isdigit() and 0 <= int(idx) - 1 < len(providers):
                delete_provider(data, int(idx) - 1)
            else:
                print("Invalid number.")
        elif choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(providers):
                edit_provider(data, n - 1)
            else:
                print("Invalid number.")
        else:
            print("Invalid input.")

    print("Bye.")


# ---------- Entry ----------
def main():
    ensure_utf8()
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        cmd_help()
        return
    cmd = argv[0]
    if cmd == "providers":
        cmd_providers()
    elif cmd == "clear":
        cmd_clear(argv[1:])
    elif cmd == "server":
        cmd_server()
    elif cmd == "all":
        cmd_all(argv[1] if len(argv) > 1 else "", argv[2:])
    elif cmd == "help":
        cmd_help()
    elif is_valid_name(cmd) and find_provider(load_data(), cmd):
        cmd_run(cmd, argv[1:])
    else:
        print(f"Unknown command or provider: {cmd}")
        cmd_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

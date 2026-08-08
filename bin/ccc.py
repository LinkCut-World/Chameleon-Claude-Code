#!/usr/bin/env python3
"""ccc - Chameleon Claude Code

A single CLI to manage Claude Code's multi-provider configuration.

Usage:
  ccc                         Show help
  ccc providers               Manage providers (add / edit / delete)
  ccc <provider> [claude args]  Start claude with a provider
  ccc clear [claude args]     Clear provider config, use default Anthropic
  ccc server                  Start the local proxy (Method 2 server)
  ccc all [claude args]       Start claude through the proxy (Method 2 client)
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


def launch_claude(args):
    """Run claude as a subprocess, inheriting this process's environment.

    Resolve claude with shutil.which (on Windows it is a .cmd installed by npm;
    subprocess.run(["claude"]) alone cannot find it, so resolve the full path).
    """
    import shutil
    exe = shutil.which("claude")
    if not exe:
        print("[ERROR] claude command not found. Install it first: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)
    return subprocess.run([exe] + list(args))


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
  ccc all [claude args]       Start claude through the proxy (Method 2 client)
  ccc help                    Show this help

Examples:
  ccc providers               # open the provider manager
  ccc my-provider -m model    # start claude with a provider
  ccc server                  # start the proxy in terminal A
  ccc all                     # start claude through the proxy in terminal B
""", end="")


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
    print(f"Starting claude with {name} ...")
    launch_claude(args)


def cmd_clear(args):
    apply_dotenv()
    for k in ANTHROPIC_VARS:
        os.environ.pop(k, None)
    print("Cleared provider config, starting claude with default Anthropic ...")
    launch_claude(args)


def cmd_server():
    apply_dotenv()
    proxy = os.path.join(BASE, "simple-proxy.py")
    print(f"Starting local proxy ... serving at {PROXY_BASE} (Ctrl+C to stop)")
    sys.exit(subprocess.run([sys.executable, proxy]).returncode)


def cmd_all(args):
    data = load_data()
    if not get_providers(data):
        print("[ERROR] No providers configured yet. Run ccc providers to add one.")
        sys.exit(1)
    if not server_up():
        print(f"[ERROR] Local proxy is not running ({PROXY_BASE}/health unreachable).")
        print("        Start it in another terminal first: ccc server")
        sys.exit(1)
    apply_dotenv()
    # Use ANTHROPIC_AUTH_TOKEN instead of ANTHROPIC_API_KEY: claude's /model
    # validation resolves auth reliably with AUTH_TOKEN; with API_KEY it can
    # report "Could not resolve authentication method". The proxy does the real
    # auth, so the value here is just a dummy.
    os.environ.pop("ANTHROPIC_API_KEY", None)  # keep only one auth variable
    os.environ["ANTHROPIC_BASE_URL"] = PROXY_BASE
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "dummy"
    print("Starting claude through the local proxy ... switch providers in-session with /model provider/model")
    launch_claude(args)


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
        cmd_all(argv[1:])
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

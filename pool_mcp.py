#!/usr/bin/env python3
"""MCP face for the GPT account pool (stdio, newline-delimited JSON-RPC).

Thin wrappers around ~/goose/pool/pool.py subcommands - the pool CLI is
the single source of truth; this server only exposes it as tools.
Starts instantly and does no network or file work at startup, so it can
never stall session creation.
"""
import json
import os
import subprocess
import sys

POOL = "/home/gmy/goose/pool/pool.py"
PANEL_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "pool_panel.html")
PANEL_URI = "ui://pool/panel"
MCP_APPS_MIME = "text/html;profile=mcp-app"

TOOLS = [
    {
        "name": "pool_status",
        "description": "Show the GPT account pool: which ChatGPT account is active, which are parked/standby, and why.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pool_usage",
        "description": "Live quota for the pooled ChatGPT accounts (5-hour and weekly windows with reset times).",
        "inputSchema": {
            "type": "object",
            "properties": {"account": {"type": "string", "description": "acct1 or acct2; omit for all"}},
        },
    },
    {
        "name": "pool_switch",
        "description": "Switch the active ChatGPT account (restarts goose-serve; sessions are preserved). The switch is verified with a live probe and rolled back automatically if the target account cannot answer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "acct1 or acct2"},
                "force": {"type": "boolean", "description": "Switch even if the account is parked/capped"},
            },
            "required": ["account"],
        },
    },
    {
        "name": "pool_park",
        "description": "Park a non-active account out of rotation (its login is kept). Optionally for a number of hours.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "hours": {"type": "number"},
            },
            "required": ["account"],
        },
    },
    {
        "name": "pool_unpark",
        "description": "Put a parked account back into rotation as standby.",
        "inputSchema": {
            "type": "object",
            "properties": {"account": {"type": "string"}},
            "required": ["account"],
        },
    },
    {
        "name": "pool_add_begin",
        "description": "Start adding a NEW ChatGPT account to the pool. Returns a device code and the URL (auth.openai.com/codex/device) the user must open to authorize the new account. Show the code and URL to the user; after they confirm they authorized it, call pool_add_finish.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pool_add_finish",
        "description": "Finish adding the account started with pool_add_begin, once the user has authorized the device code. If the user has not authorized yet it says so; ask them to finish and call again. The new account joins as standby with its own isolated authorization.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pool_remove",
        "description": "Remove an account from the pool and delete its stored credentials from the Pi. Only when the user explicitly asks to remove that account. Refuses to remove the currently active account (switch first).",
        "inputSchema": {
            "type": "object",
            "properties": {"account": {"type": "string", "description": "acct1, acct2, ..."}},
            "required": ["account"],
        },
    },
    {
        "name": "pool_panel",
        "description": "Open the interactive GPT account pool panel in the chat: accounts, quota, switch / park / add / remove with buttons. Call this when the user asks to see or manage the account pool with a panel.",
        "inputSchema": {"type": "object", "properties": {}},
        "_meta": {"ui": {"resourceUri": PANEL_URI}},
    },
    {
        "name": "pool_data",
        "description": "Pool status as structured JSON (used by the pool panel; also handy for precise answers).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pool_log",
        "description": "Recent account-pool events (switches, parks, patrol findings).",
        "inputSchema": {
            "type": "object",
            "properties": {"lines": {"type": "number", "description": "How many lines, default 20"}},
        },
    },
]


def run_pool(args, timeout=560):
    p = subprocess.run(
        ["python3", POOL] + args,
        capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    text = out if out else err
    return text or "(no output)", p.returncode != 0


def pool_data_payload():
    p = subprocess.run(["python3", POOL, "status-json"],
                       capture_output=True, text=True, timeout=120)
    return json.loads(p.stdout.strip().splitlines()[-1])


def call_tool_full(name, arguments):
    """Tools that return a full CallToolResult (with structuredContent)
    instead of plain text. Returns None for plain-text tools."""
    if name not in ("pool_panel", "pool_data"):
        return None
    try:
        data = pool_data_payload()
    except Exception as e:
        return {"content": [{"type": "text",
                             "text": f"pool data failed: {e}"}],
                "isError": True}
    active = data.get("active")
    return {
        "content": [{"type": "text",
                     "text": f"GPT account pool: {len(data.get('accounts', []))} "
                             f"accounts, active = {active}. "
                             "The interactive panel carries the details."}],
        "structuredContent": data,
        "isError": False,
    }


def call_tool(name, arguments):
    a = arguments or {}
    if name == "pool_status":
        return run_pool(["status"], 60)
    if name == "pool_usage":
        args = ["usage"] + ([a["account"]] if a.get("account") else [])
        return run_pool(args, 120)
    if name == "pool_switch":
        args = ["switch", a.get("account", "")]
        if a.get("force"):
            args.append("--force")
        return run_pool(args)
    if name == "pool_park":
        args = ["park", a.get("account", "")]
        if a.get("hours"):
            args.append(str(a["hours"]))
        return run_pool(args, 60)
    if name == "pool_unpark":
        return run_pool(["unpark", a.get("account", "")], 60)
    if name == "pool_add_begin":
        return run_pool(["add-begin"], 60)
    if name == "pool_add_finish":
        return run_pool(["add-finish"], 120)
    if name == "pool_remove":
        return run_pool(["remove", a.get("account", "")], 60)
    if name == "pool_log":
        return run_pool(["log", str(int(a.get("lines", 20)))], 60)
    return f"unknown tool {name}", True


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method", "")
        mid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "account-pool", "version": "1.2.0"},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            full = None
            try:
                full = call_tool_full(params.get("name", ""),
                                      params.get("arguments"))
                if full is None:
                    text, is_err = call_tool(params.get("name", ""),
                                             params.get("arguments"))
                    full = {"content": [{"type": "text", "text": text}],
                            "isError": is_err}
            except subprocess.TimeoutExpired:
                full = {"content": [{"type": "text",
                                     "text": "pool command timed out"}],
                        "isError": True}
            except Exception as e:
                full = {"content": [{"type": "text",
                                     "text": f"pool tool error: {type(e).__name__}: {str(e)[:200]}"}],
                        "isError": True}
            send({"jsonrpc": "2.0", "id": mid, "result": full})
        elif method == "resources/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"resources": [{
                "uri": PANEL_URI,
                "name": "GPT account pool panel",
                "title": "GPT account pool panel",
                "description": "Interactive panel for the GPT account pool: accounts, quota, switch / park / add / remove.",
                "mimeType": MCP_APPS_MIME,
            }]}})
        elif method == "resources/read":
            uri = (msg.get("params") or {}).get("uri", "")
            if uri != PANEL_URI:
                send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32602, "message": f"unknown resource {uri}"}})
            else:
                try:
                    with open(PANEL_HTML_PATH) as f:
                        html = f.read()
                    send({"jsonrpc": "2.0", "id": mid, "result": {
                        "contents": [{
                            "uri": PANEL_URI,
                            "mimeType": MCP_APPS_MIME,
                            "text": html,
                            "_meta": {"ui": {"prefersBorder": True}},
                        }]}})
                except Exception as e:
                    send({"jsonrpc": "2.0", "id": mid, "error": {
                        "code": -32603,
                        "message": f"panel html unavailable: {e}"}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()

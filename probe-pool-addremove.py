#!/usr/bin/env python3
"""Verify the pool extension's new tools end-to-end over ACP:
fresh session -> tools/list shows 9 pool tools -> call
pool__pool_remove on the planted dummy acct8 -> confirm removal."""
import importlib.util
import json

spec = importlib.util.spec_from_file_location(
    "followup", "/home/gmy/goose/test-recall-followup.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def main():
    c = mod.Client()
    c.request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": False,
                                      "writeTextFile": False},
                               "terminal": False},
        "clientInfo": {"name": "probe-pool-addremove", "version": "1.0"}})
    r, _ = c.request("session/new", {"cwd": "/home/gmy",
                                     "mcpServers": []})
    sid = r["sessionId"]
    print("session:", sid)
    try:
        r2, _ = c.request("_goose/unstable/tools/list",
                          {"sessionId": sid})
        tools = r2.get("tools", r2) if isinstance(r2, dict) else r2
        names = sorted(t.get("name", "") for t in tools
                       if isinstance(t, dict))
        pool_tools = [n for n in names if "pool" in n]
        print(f"total tools: {len(names)}; pool tools ({len(pool_tools)}):")
        for n in pool_tools:
            print("  ", n)
        r3, _ = c.request("_goose/unstable/tools/call", {
            "sessionId": sid, "extensionName": "pool",
            "name": "pool__pool_remove",
            "arguments": {"account": "acct8"},
        })
        print("pool_remove result:",
              json.dumps(r3, ensure_ascii=False)[:400])
    finally:
        try:
            c.request("session/delete", {"sessionId": sid})
            print("session deleted")
        except Exception as e:
            print("delete failed:", e)


if __name__ == "__main__":
    main()

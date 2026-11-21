#!/usr/bin/env python3
"""Verify the pool extension inside a real goose-serve session:
new session -> tools/list contains pool tools -> call pool_status ->
delete the test session."""
import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "followup", "/home/gmy/goose/test-recall-followup.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Client = _mod.Client


def main():
    c = Client()
    c.request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        "clientInfo": {"name": "pool-probe", "version": "1.0"},
    })
    r, _ = c.request("session/new", {"cwd": "/home/gmy", "mcpServers": []})
    sid = r["sessionId"]
    print("session:", sid, flush=True)
    try:
        r2, _ = c.request("_goose/unstable/tools/list", {"sessionId": sid})
        tools = r2.get("tools", r2 if isinstance(r2, list) else [])
        names = [t.get("name", "") for t in tools]
        pool_tools = [n for n in names if "pool" in n]
        print("total tools:", len(names), "pool tools:", pool_tools, flush=True)
        r3, _ = c.request("_goose/unstable/tools/call", {
            "sessionId": sid, "extensionName": "pool",
            "name": "pool__pool_status", "arguments": {}})
        print("pool_status call:", str(r3)[:300], flush=True)
    finally:
        try:
            c.request("session/delete", {"sessionId": sid})
            print("test session deleted", flush=True)
        except Exception as e:
            print("delete failed:", e, flush=True)


if __name__ == "__main__":
    main()

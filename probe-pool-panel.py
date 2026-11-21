#!/usr/bin/env python3
"""Verify goose-serve attaches the MCP app meta for pool_panel:
fresh session -> tools/call pool__pool_panel -> look for
_meta.goose.mcpApp (resourceUri + embedded HTML) in the result or
in streamed session/update chunks."""
import importlib.util
import json

spec = importlib.util.spec_from_file_location(
    "followup", "/home/gmy/goose/test-recall-followup.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def find_mcpapp(obj, path="$"):
    hits = []
    if isinstance(obj, dict):
        if "mcpApp" in obj:
            hits.append((path, obj["mcpApp"]))
        for k, v in obj.items():
            hits += find_mcpapp(v, path + "." + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += find_mcpapp(v, path + f"[{i}]")
    return hits


def main():
    c = mod.Client()
    c.request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": False,
                                      "writeTextFile": False},
                               "terminal": False},
        "clientInfo": {"name": "probe-pool-panel", "version": "1.0"}})
    r, _ = c.request("session/new", {"cwd": "/home/gmy",
                                     "mcpServers": []})
    sid = r["sessionId"]
    print("session:", sid, flush=True)
    try:
        r3, chunks = c.request("_goose/unstable/tools/call", {
            "sessionId": sid, "extensionName": "pool",
            "name": "pool__pool_panel", "arguments": {}})
        hits = find_mcpapp(r3, "result") + find_mcpapp(chunks, "chunks")
        print("mcpApp hits:", len(hits))
        for path, app in hits[:3]:
            if isinstance(app, dict):
                rr = app.get("resourceResult") or {}
                text = ""
                try:
                    text = rr["contents"][0].get("text", "")
                except Exception:
                    text = str(rr)[:0]
                print(f"  at {path}: toolName={app.get('toolName')} "
                      f"extensionName={app.get('extensionName')} "
                      f"resourceUri={app.get('resourceUri')} "
                      f"html_bytes={len(text)} "
                      f"html_has_panel={'GPT 账号池' in text}")
            else:
                print(f"  at {path}: {str(app)[:200]}")
        print("result content head:",
              json.dumps(r3, ensure_ascii=False)[:300])
    finally:
        try:
            c.request("session/delete", {"sessionId": sid})
            print("session deleted")
        except Exception as e:
            print("delete failed:", e)


if __name__ == "__main__":
    main()

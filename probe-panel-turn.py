#!/usr/bin/env python3
"""Real-turn verification: ask the agent to call pool_panel and scan
the raw session/update notifications for _meta.goose.mcpApp."""
import importlib.util
import json

spec = importlib.util.spec_from_file_location(
    "followup", "/home/gmy/goose/test-recall-followup.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class RawClient(mod.Client):
    def __init__(self):
        super().__init__()
        self.raw_updates = []

    def request(self, method, params, timeout=600):
        self.next_id += 1
        rid = self.next_id
        self.s.settimeout(timeout)
        mod.ws_send(self.s, {"jsonrpc": "2.0", "id": rid,
                             "method": method, "params": params})
        chunks = []
        while True:
            msg = json.loads(self._read_frame())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method} error: {msg['error']}")
                return msg.get("result"), chunks
            if "method" in msg and "id" in msg:
                mod.ws_send(self.s, {"jsonrpc": "2.0", "id": msg["id"],
                                     "error": {"code": -32601,
                                               "message": "not supported"}})
                continue
            if msg.get("method") == "session/update":
                upd = msg["params"].get("update", {})
                kind = upd.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    c = upd.get("content") or {}
                    if c.get("type") == "text":
                        chunks.append(c["text"])
                elif kind in ("tool_call", "tool_call_update"):
                    self.raw_updates.append(upd)


def find_mcpapp(obj):
    hits = []
    if isinstance(obj, dict):
        if "mcpApp" in obj:
            hits.append(obj["mcpApp"])
        for v in obj.values():
            hits += find_mcpapp(v)
    elif isinstance(obj, list):
        for v in obj:
            hits += find_mcpapp(v)
    return hits


def main():
    c = RawClient()
    c.request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
            "_meta": {"goose": {"mcpHostCapabilities": {"extensions": {
                "io.modelcontextprotocol/ui": {
                    "mimeTypes": ["text/html;profile=mcp-app"]}}}}},
        },
        "clientInfo": {"name": "probe-panel-turn", "version": "1.0"}})
    r, _ = c.request("session/new", {"cwd": "/home/gmy",
                                     "mcpServers": []})
    sid = r["sessionId"]
    print("session:", sid, flush=True)
    try:
        c.request("session/prompt", {
            "sessionId": sid,
            "prompt": [{"type": "text", "text":
                        "Call the pool_panel tool exactly once now "
                        "(from the pool extension). After it returns, "
                        "reply with just: DONE"}]}, timeout=420)
        print("tool updates captured:", len(c.raw_updates), flush=True)
        for u in c.raw_updates:
            print("UPDATE:", json.dumps(u, ensure_ascii=False)[:900],
                  flush=True)
        r2, _ = c.request("_goose/unstable/tools/list",
                          {"sessionId": sid})
        tools = r2.get("tools", r2) if isinstance(r2, dict) else r2
        for t in tools:
            if isinstance(t, dict) and "pool_panel" in str(t.get("name", "")):
                print("ACP tools/list pool_panel entry:",
                      json.dumps(t, ensure_ascii=False)[:600], flush=True)
        hits = find_mcpapp(c.raw_updates)
        print("mcpApp hits:", len(hits), flush=True)
        for app in hits[:2]:
            if isinstance(app, dict):
                rr = app.get("resourceResult") or {}
                text = ""
                try:
                    text = rr["contents"][0].get("text", "")
                except Exception:
                    pass
                print(f"  toolName={app.get('toolName')} "
                      f"extensionName={app.get('extensionName')} "
                      f"resourceUri={app.get('resourceUri')} "
                      f"html_bytes={len(text)} "
                      f"html_has_panel={'GPT 账号池' in text} "
                      f"html_has_bridge={'ui/initialize' in text}")
    finally:
        try:
            c.request("session/delete", {"sessionId": sid})
            print("session deleted")
        except Exception as e:
            print("delete failed:", e)


if __name__ == "__main__":
    main()

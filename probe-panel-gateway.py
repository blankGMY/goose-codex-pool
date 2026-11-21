#!/usr/bin/env python3
"""End-to-end web-path verification: log in to the goose-web gateway
(127.0.0.1:8083), open its /acp WebSocket with the gate cookie, then
initialize EXACTLY like the current acp-ui web build does (including
the mcpHostCapabilities declaration), start a session, have the agent
call pool_panel, and scan the tool updates for _meta.goose.mcpApp."""
import base64
import json
import os
import socket
import struct

GW = ("127.0.0.1", 8083)
SECRET = open(os.path.expanduser("~/goose/goose-serve-secret.txt")).read().strip()


def http_login():
    body = ("password=" + SECRET).encode()
    req = (b"POST /login HTTP/1.1\r\nHost: 127.0.0.1:8083\r\n"
           b"Content-Type: application/x-www-form-urlencoded\r\n"
           b"Content-Length: " + str(len(body)).encode() +
           b"\r\nConnection: close\r\n\r\n" + body)
    s = socket.create_connection(GW, timeout=15)
    s.sendall(req)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head = data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
    cookie = None
    for line in head.split("\r\n"):
        if line.lower().startswith("set-cookie:") and "goose_gate=" in line:
            cookie = line.split("goose_gate=", 1)[1].split(";", 1)[0]
    if not cookie:
        raise RuntimeError("login failed: " + head.split("\r\n", 1)[0])
    return cookie


class WS:
    def __init__(self, cookie):
        self.s = socket.create_connection(GW, timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /acp HTTP/1.1\r\nHost: 127.0.0.1:8083\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Cookie: goose_gate={cookie}\r\n\r\n")
        self.s.sendall(req.encode())
        self.buf = b""
        head = self._read_until(b"\r\n\r\n").decode("iso-8859-1", "replace")
        if "101" not in head.split("\r\n", 1)[0]:
            raise RuntimeError("ws upgrade failed: " + head.split("\r\n", 1)[0])

    def _read_until(self, delim):
        while delim not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        idx = self.buf.index(delim) + len(delim)
        out, self.buf = self.buf[:idx], self.buf[idx:]
        return out

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        ln = len(payload)
        if ln < 126:
            hdr = bytes([0x81, 0x80 | ln])
        elif ln < 65536:
            hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", ln)
        else:
            hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", ln)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.s.sendall(hdr + mask + masked)

    def recv(self):
        while True:
            b1, b2 = self._read_exact(2)
            opcode = b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            payload = self._read_exact(ln) if ln else b""
            if opcode == 9:
                continue
            if opcode in (1, 2):
                return payload.decode("utf-8", "replace")


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.raw_updates = []

    def request(self, method, params, timeout=420):
        self.next_id += 1
        rid = self.next_id
        self.ws.s.settimeout(timeout)
        self.ws.send({"jsonrpc": "2.0", "id": rid,
                      "method": method, "params": params})
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method} error: {msg['error']}")
                return msg.get("result")
            if "method" in msg and "id" in msg:
                self.ws.send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32601, "message": "not supported"}})
                continue
            if msg.get("method") == "session/update":
                upd = msg["params"].get("update", {})
                if upd.get("sessionUpdate") in ("tool_call",
                                                "tool_call_update"):
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
    cookie = http_login()
    print("gateway login ok", flush=True)
    c = Client(WS(cookie))
    # Exactly what the current acp-ui build sends (session.ts).
    c.request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "_meta": {"goose": {"mcpHostCapabilities": {"extensions": {
                "io.modelcontextprotocol/ui": {
                    "mimeTypes": ["text/html;profile=mcp-app"]}}}}},
        },
        "clientInfo": {"name": "acp-ui", "title": "ACP UI",
                       "version": "0.1.16"}})
    print("initialize ok (via gateway)", flush=True)
    r = c.request("session/new", {"cwd": "/home/gmy", "mcpServers": []})
    sid = r["sessionId"]
    print("session:", sid, flush=True)
    try:
        c.request("session/prompt", {
            "sessionId": sid,
            "prompt": [{"type": "text", "text":
                        "Call the pool_panel tool exactly once now "
                        "(from the pool extension). After it returns, "
                        "reply with just: DONE"}]})
        hits = find_mcpapp(c.raw_updates)
        print("tool updates:", len(c.raw_updates),
              "| mcpApp hits:", len(hits), flush=True)
        for app in hits[:1]:
            if isinstance(app, dict):
                rr = app.get("resourceResult") or {}
                text = ""
                try:
                    text = rr["contents"][0].get("text", "")
                except Exception:
                    pass
                print(f"  toolName={app.get('toolName')} "
                      f"resourceUri={app.get('resourceUri')} "
                      f"html_bytes={len(text)} "
                      f"html_has_panel={'GPT 账号池' in text}")
    finally:
        try:
            c.request("session/delete", {"sessionId": sid})
            print("session deleted")
        except Exception as e:
            print("delete failed:", e)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""GPT account pool for the Pi Goose hub.

Two ChatGPT (Codex) accounts, one active at a time. The pool keeps a
full credential set per account, watches the active account's quota
via the wham/usage endpoint, parks an exhausted/invalid account until
its reset, and fails over to a healthy standby by swapping the token
files Goose reads and restarting goose-serve (sessions live in the
server DB, so a restart loses nothing).

Safety rules:
- Never print token values; account ids are masked in output.
- All file writes are atomic (tmp + rename), mode 600.
- A switch is only kept if a live probe (goose run, Codex provider)
  answers; otherwise the previous credentials are restored.
- If no healthy account exists, the current files are left untouched.

State: ~/goose/pool/pool.json; per-account credentials in
~/goose/pool/accounts/<id>/{auth.json,tokens.json}; event log in
~/goose/pool/pool.log.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
POOL_DIR = os.path.join(HOME, "goose", "pool")
POOL_JSON = os.path.join(POOL_DIR, "pool.json")
ACCOUNTS_DIR = os.path.join(POOL_DIR, "accounts")
LOG_PATH = os.path.join(POOL_DIR, "pool.log")
LOCK_PATH = os.path.join(POOL_DIR, ".lock")
GOOSE_TOKENS = os.path.join(HOME, ".config", "goose", "chatgpt_codex", "tokens.json")
CODEX_AUTH = os.path.join(HOME, ".codex", "auth.json")
GOOSE_BIN = os.path.join(HOME, "goose", "goose")
PROBE_MODEL = "gpt-6.2-sol"

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
PROXY = "http://127.0.0.1:7890"

PENDING_ADD = os.path.join(POOL_DIR, "pending-add.json")
DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
DEVICE_VERIFY_URL = "https://auth.openai.com/codex/device"
DEVICE_UA = "codex-cli/0.1.0"

LIMIT_SIGNATURES = ("usage limit", "rate_limit", "rate limit", "too many requests", "429")
INVALID_SIGNATURES = ("invalidated", "invalid_grant", "401", "unauthorized", "token_invalidated")


# ---------- small helpers ----------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ts():
    return time.time()


def mask(account_id):
    return (account_id or "?")[:8] + "…"


def log_event(kind, text):
    line = f"{now_iso()} [{kind}] {text}"
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def atomic_write(path, data_bytes, mode=0o600):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data_bytes)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save_json(path, obj):
    atomic_write(path, json.dumps(obj, indent=1).encode())


def load_json(path):
    with open(path) as f:
        return json.load(f)


class Lock:
    def __enter__(self):
        for _ in range(60):
            try:
                fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(LOCK_PATH) > 180:
                        os.unlink(LOCK_PATH)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(2)
        raise SystemExit("pool is busy (lock held)")

    def __exit__(self, *a):
        try:
            os.unlink(LOCK_PATH)
        except FileNotFoundError:
            pass


def load_pool():
    return load_json(POOL_JSON)


def save_pool(pool):
    save_json(POOL_JSON, pool)


def account(pool, acct_id):
    for a in pool["accounts"]:
        if a["id"] == acct_id:
            return a
    return None


def acct_paths(acct_id):
    d = os.path.join(ACCOUNTS_DIR, acct_id)
    return os.path.join(d, "auth.json"), os.path.join(d, "tokens.json")


def opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


# ---------- provider calls ----------

def harvest_active(pool):
    """Copy the live credential files back into the active account's
    pool store. Goose itself is the ONLY refresher of the live grant;
    the pool must never refresh the active account from its stored
    copy (two refreshers on one token family trip OpenAI's reuse
    detection and get the whole family revoked)."""
    acct = account(pool, pool["active"])
    if not acct:
        return
    auth_path, tokens_path = acct_paths(acct["id"])
    try:
        live = open(GOOSE_TOKENS, "rb").read()
        tok = json.loads(live.decode())
        if not tok.get("refresh_token"):
            return
        if os.path.exists(tokens_path) and open(tokens_path, "rb").read() == live:
            return
        atomic_write(tokens_path, live)
        auth = load_json(auth_path)
        auth.setdefault("tokens", {}).update({
            "access_token": tok.get("access_token"),
            "refresh_token": tok.get("refresh_token"),
            "id_token": tok.get("id_token"),
            "account_id": tok.get("account_id"),
        })
        save_json(auth_path, auth)
    except Exception:
        pass


def refresh_account(acct_id, pool=None):
    """Use the account's refresh_token to mint fresh tokens; write its
    pool-side auth.json/tokens.json. Returns (ok, detail).
    Only ever called for PARKED accounts, where the pool is the sole
    refresher - and never for accounts whose grant is shared with the
    PC's own Codex login."""
    if pool is not None:
        a = account(pool, acct_id)
        if a and a.get("grant_shared"):
            return False, "shared grant: the Pi must not refresh this account"
    auth_path, tokens_path = acct_paths(acct_id)
    auth = load_json(auth_path)
    toks = auth.get("tokens", {})
    refresh = toks.get("refresh_token")
    if not refresh:
        return False, "no refresh_token"
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        with opener().open(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:160]
        except Exception:
            pass
        return False, f"HTTP {e.code} {detail}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    access = body.get("access_token")
    if not access:
        return False, "no access_token in response"
    new_refresh = body.get("refresh_token", refresh)
    new_id = body.get("id_token", toks.get("id_token"))
    expires_at = datetime.fromtimestamp(
        now_ts() + int(body.get("expires_in", 3600)), timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    token_data = {
        "access_token": access,
        "refresh_token": new_refresh,
        "id_token": new_id,
        "expires_at": expires_at,
        "account_id": toks.get("account_id"),
    }
    save_json(tokens_path, token_data)
    auth["tokens"]["access_token"] = access
    auth["tokens"]["refresh_token"] = new_refresh
    if new_id:
        auth["tokens"]["id_token"] = new_id
    auth["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    save_json(auth_path, auth)
    return True, f"expires_at={expires_at}"


def fetch_usage(acct_id):
    """Query wham/usage with the account's stored access token.
    Returns (status, data): status in ok|invalid|error; data has
    plan_type, primary{used,reset}, secondary{used,reset},
    limit_reached, allowed."""
    _, tokens_path = acct_paths(acct_id)
    tok = load_json(tokens_path)
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok["access_token"],
        "ChatGPT-Account-Id": tok.get("account_id") or "",
        "User-Agent": "codex-cli/0.1.0",
    })
    try:
        with opener().open(req, timeout=25) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "invalid", {"error": f"HTTP {e.code}"}
        return "error", {"error": f"HTTP {e.code}"}
    except Exception as e:
        return "error", {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    rl = body.get("rate_limit") or {}

    def win(name):
        w = rl.get(name) or {}
        reset = w.get("reset_at")
        if reset is None and w.get("reset_after_seconds") is not None:
            reset = now_ts() + float(w["reset_after_seconds"])
        return {"used": w.get("used_percent"), "reset": reset}

    return "ok", {
        "plan_type": body.get("plan_type"),
        "primary": win("primary_window"),
        "secondary": win("secondary_window"),
        "limit_reached": bool(rl.get("limit_reached")),
        "allowed": rl.get("allowed"),
    }


def ensure_fresh(pool, acct_id):
    """Make sure the account's stored access token is usable.
    Active account: harvest Goose's live tokens (never refresh).
    Parked + shared grant: never refresh; an expired access token
    just means 'stale' until the account is re-authorized on the Pi.
    Parked + own grant: lazy refresh when near expiry."""
    if acct_id == pool["active"]:
        harvest_active(pool)
        return True, "harvested"
    _, tokens_path = acct_paths(acct_id)
    try:
        tok = load_json(tokens_path)
        exp = tok.get("expires_at", "")
        exp_ts = datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        exp_ts = 0
    if exp_ts - now_ts() < 180:
        a = account(pool, acct_id)
        if a and a.get("grant_shared"):
            return False, "stale shared grant (re-authorize on the Pi to fix)"
        return refresh_account(acct_id, pool)
    return True, "fresh"


def usage_health(pool, acct_id):
    """(health, usage): health in ok|capped|invalid|unknown."""
    ok, detail = ensure_fresh(pool, acct_id)
    if not ok:
        if "stale shared grant" in detail:
            return "unknown", {"error": detail}
        if any(s in detail.lower() for s in INVALID_SIGNATURES):
            return "invalid", {"error": detail}
        return "unknown", {"error": detail}
    status, data = fetch_usage(acct_id)
    if status == "invalid":
        return "invalid", data
    if status != "ok":
        return "unknown", data
    capped = data["limit_reached"] or data["allowed"] is False or \
        (data["primary"]["used"] is not None and data["primary"]["used"] >= 100)
    return ("capped" if capped else "ok"), data


def cap_reset_ts(usage):
    """When a capped account frees up: the max reset among windows at
    100%, else the primary reset, else +1h."""
    resets = []
    for w in (usage.get("primary") or {}, usage.get("secondary") or {}):
        if w.get("used") is not None and w["used"] >= 100 and w.get("reset"):
            resets.append(w["reset"])
    if resets:
        return max(resets)
    if (usage.get("primary") or {}).get("reset"):
        return usage["primary"]["reset"]
    return now_ts() + 3600


# ---------- switching ----------

def restart_goose():
    subprocess.run(["sudo", "-n", "systemctl", "restart", "goose-serve"],
                   capture_output=True, timeout=60)
    for _ in range(30):
        try:
            req = urllib.request.Request("http://127.0.0.1:3284/status")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def probe_active():
    """Ask the Codex provider for a PONG via goose run.
    Returns (ok, detail)."""
    try:
        p = subprocess.run(
            [GOOSE_BIN, "run", "--provider", "chatgpt_codex",
             "--model", PROBE_MODEL, "--max-turns", "1",
             "--text", "Reply with exactly: PONG"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "GOOSE_TELEMETRY_ENABLED": "false",
                 "HTTP_PROXY": PROXY, "HTTPS_PROXY": PROXY,
                 "http_proxy": PROXY, "https_proxy": PROXY,
                 "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"})
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        if "PONG" in p.stdout:
            return True, "PONG"
        low = out.lower()
        if any(s in low for s in LIMIT_SIGNATURES):
            return False, "limit: " + out.strip()[-200:]
        if any(s in low for s in INVALID_SIGNATURES):
            return False, "invalid: " + out.strip()[-200:]
        return False, "no PONG (rc=%s): %s" % (p.returncode, out.strip()[-200:])
    except subprocess.TimeoutExpired:
        return False, "probe timeout"
    except Exception as e:
        return False, f"probe error: {type(e).__name__}: {str(e)[:120]}"


def install_account_files(acct_id):
    auth_path, tokens_path = acct_paths(acct_id)
    atomic_write(GOOSE_TOKENS, open(tokens_path, "rb").read())
    atomic_write(CODEX_AUTH, open(auth_path, "rb").read())


def do_switch(pool, target_id, reason, probe=True):
    """Switch the live credentials to target_id. On probe failure the
    previous account's files are restored. Returns (ok, detail)."""
    current_id = pool["active"]
    if target_id == current_id:
        return True, "already active"
    target = account(pool, target_id)
    if not target:
        return False, f"unknown account {target_id}"
    harvest_active(pool)
    install_account_files(target_id)
    if not restart_goose():
        install_account_files(current_id)
        restart_goose()
        return False, "goose-serve did not come back after switch; rolled back"
    if probe:
        ok, detail = probe_active()
        if not ok:
            install_account_files(current_id)
            restart_goose()
            kind = "limit" if detail.startswith("limit") else \
                "invalid" if detail.startswith("invalid") else "error"
            if kind in ("limit", "invalid"):
                target["state"] = "parked"
                target["park_reason"] = "capped" if kind == "limit" else "invalid"
                if kind == "limit":
                    _, u = usage_health(pool, target_id)
                    target["parked_until"] = cap_reset_ts(u) if u else now_ts() + 3600
                else:
                    target["parked_until"] = None
                target["last_error"] = detail
            save_pool(pool)
            return False, f"probe failed ({detail}); rolled back to {current_id}"
    prev = account(pool, current_id)
    if prev and prev["state"] == "active":
        prev["state"] = "standby"
    target["state"] = "active"
    target["parked_until"] = None
    target["park_reason"] = None
    target["last_error"] = None
    pool["active"] = target_id
    save_pool(pool)
    log_event("switch", f"{current_id} -> {target_id} ({reason})")
    return True, f"active={target_id}"


def failover(pool, failed_id, reason):
    """Pick the best eligible account and switch to it."""
    candidates = [a for a in pool["accounts"] if a["id"] != failed_id]
    candidates.sort(key=lambda a: a["priority"])
    for cand in candidates:
        if cand["state"] == "parked":
            if cand["park_reason"] != "capped":
                continue
            if cand["parked_until"] and cand["parked_until"] > now_ts():
                continue
        health, usage = usage_health(pool, cand["id"])
        cand["last_check"] = now_iso()
        if health != "ok":
            if health == "capped":
                cand["state"] = "parked"
                cand["park_reason"] = "capped"
                cand["parked_until"] = cap_reset_ts(usage)
            elif health == "invalid":
                cand["state"] = "parked"
                cand["park_reason"] = "invalid"
                cand["parked_until"] = None
            save_pool(pool)
            continue
        return do_switch(pool, cand["id"], f"failover from {failed_id}: {reason}")
    return False, "no healthy account available"


# ---------- commands ----------

def cmd_init():
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    if os.path.exists(POOL_JSON):
        print("pool already initialized")
        return
    sources = {
        "acct2": (CODEX_AUTH, GOOSE_TOKENS, 1, "active"),
        "acct1": (os.path.join(HOME, "goose", "auth-backup-acct1.json"),
                  os.path.join(HOME, "goose", "tokens-backup-acct1.json"),
                  2, "parked"),
    }
    accounts = []
    for acct_id, (auth_src, tok_src, prio, state) in sources.items():
        auth_path, tokens_path = acct_paths(acct_id)
        os.makedirs(os.path.dirname(auth_path), exist_ok=True)
        atomic_write(auth_path, open(auth_src, "rb").read())
        atomic_write(tokens_path, open(tok_src, "rb").read())
        auth = load_json(auth_path)
        accounts.append({
            "id": acct_id,
            "account_id": auth.get("tokens", {}).get("account_id"),
            "priority": prio,
            "state": state,
            "parked_until": None,
            "park_reason": "capped" if state == "parked" else None,
            "last_error": None,
            "last_check": None,
        })
    pool = {"version": 1, "active": "acct2", "accounts": accounts}
    save_pool(pool)
    log_event("init", "pool initialized: acct2 active, acct1 parked (capped, to be probed)")
    print("initialized")


def fmt_until(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M UTC")


def cmd_status(pool):
    print(f"active: {pool['active']}")
    for a in sorted(pool["accounts"], key=lambda x: x["priority"]):
        line = (f"  {a['id']}  acct={mask(a['account_id'])}  prio={a['priority']}  "
                f"state={a['state']}")
        if a["state"] == "parked":
            line += f"  reason={a['park_reason']} until={fmt_until(a['parked_until'])}"
        if a.get("last_error"):
            line += f"  last_error={a['last_error'][:80]}"
        print(line)


def cmd_usage(pool, acct_id=None):
    targets = [account(pool, acct_id)] if acct_id else pool["accounts"]
    for a in targets:
        if not a:
            print(f"unknown account {acct_id}")
            continue
        health, usage = usage_health(pool, a["id"])
        a["last_check"] = now_iso()
        if health in ("ok", "capped"):
            p, s = usage["primary"], usage["secondary"]
            print(f"{a['id']} ({mask(a['account_id'])}) plan={usage['plan_type']} "
                  f"health={health} 5h={p['used']}% reset={fmt_until(p['reset'])} "
                  f"week={s['used']}% reset={fmt_until(s['reset'])}")
        else:
            print(f"{a['id']} ({mask(a['account_id'])}) health={health} {usage.get('error', '')}")
    save_pool(pool)


def cmd_refresh(pool, which):
    targets = pool["accounts"] if which == "all" else [account(pool, which)]
    for a in targets:
        if not a:
            print(f"unknown account {which}")
            continue
        if a["id"] == pool["active"]:
            harvest_active(pool)
            print(f"{a['id']}: skipped (active - Goose refreshes it; pool harvested the live copy)")
            continue
        ok, detail = refresh_account(a["id"], pool)
        print(f"{a['id']}: {'ok' if ok else 'FAILED'} {detail}")
        if not ok and any(s in detail.lower() for s in INVALID_SIGNATURES):
            if a["state"] != "active":
                a["state"] = "parked"
                a["park_reason"] = "invalid"
                a["parked_until"] = None
            a["last_error"] = f"refresh failed: {detail}"
            log_event("refresh", f"{a['id']} refresh failed, marked invalid: {detail[:100]}")
    save_pool(pool)


def cmd_switch(pool, target_id, force):
    target = account(pool, target_id)
    if not target:
        print(f"unknown account {target_id}")
        return
    if target["state"] == "parked" and not force:
        print(f"{target_id} is parked ({target['park_reason']} until "
              f"{fmt_until(target['parked_until'])}); use --force to switch anyway")
        return
    if not force:
        health, usage = usage_health(pool, target_id)
        if health == "capped":
            print(f"{target_id} is capped until {fmt_until(cap_reset_ts(usage))}; not switching (use --force)")
            return
        if health == "invalid":
            print(f"{target_id} login is invalid; not switching")
            return
    with Lock():
        ok, detail = do_switch(pool, target_id, "manual switch", probe=True)
    print(("OK " if ok else "FAILED ") + detail)


def cmd_park(pool, acct_id, until_hours, reason):
    a = account(pool, acct_id)
    if not a:
        print(f"unknown account {acct_id}")
        return
    if a["state"] == "active":
        print("cannot park the active account; switch first")
        return
    a["state"] = "parked"
    a["park_reason"] = reason or "manual"
    a["parked_until"] = now_ts() + until_hours * 3600 if until_hours else None
    save_pool(pool)
    log_event("park", f"{acct_id} parked ({a['park_reason']}) until {fmt_until(a['parked_until'])}")
    print(f"{acct_id} parked")


def cmd_unpark(pool, acct_id):
    a = account(pool, acct_id)
    if not a:
        print(f"unknown account {acct_id}")
        return
    a["state"] = "standby"
    a["park_reason"] = None
    a["parked_until"] = None
    save_pool(pool)
    log_event("unpark", f"{acct_id} back in rotation (standby)")
    print(f"{acct_id} unparked (standby)")


def cmd_patrol(pool):
    changed = False
    harvest_active(pool)
    active = account(pool, pool["active"])
    health, usage = usage_health(pool, active["id"])
    active["last_check"] = now_iso()
    if health == "capped":
        reset = cap_reset_ts(usage)
        active["state"] = "parked"
        active["park_reason"] = "capped"
        active["parked_until"] = reset
        active["last_error"] = "usage limit reached"
        save_pool(pool)
        log_event("patrol", f"{active['id']} capped; parked until {fmt_until(reset)}; failing over")
        with Lock():
            ok, detail = failover(pool, active["id"], "active capped")
        log_event("patrol", f"failover: {'ok' if ok else 'FAILED'} {detail}")
        changed = True
    elif health == "invalid":
        active["state"] = "parked"
        active["park_reason"] = "invalid"
        active["parked_until"] = None
        active["last_error"] = usage.get("error", "invalid token")
        save_pool(pool)
        log_event("patrol", f"{active['id']} token invalid; parked; failing over")
        with Lock():
            ok, detail = failover(pool, active["id"], "active invalid")
        log_event("patrol", f"failover: {'ok' if ok else 'FAILED'} {detail}")
        changed = True
    # Re-probe parked accounts (early recovery / reset reached).
    pool = load_pool()
    for a in pool["accounts"]:
        if a["state"] != "parked" or a["park_reason"] != "capped":
            continue
        due = a["parked_until"] and a["parked_until"] <= now_ts()
        health2, usage2 = usage_health(pool, a["id"])
        a["last_check"] = now_iso()
        if health2 == "ok":
            a["state"] = "standby"
            a["park_reason"] = None
            a["parked_until"] = None
            a["last_error"] = None
            log_event("patrol", f"{a['id']} recovered; back in rotation (standby)")
            changed = True
        elif health2 == "capped":
            new_reset = cap_reset_ts(usage2)
            if new_reset != a["parked_until"]:
                a["parked_until"] = new_reset
                changed = True
        elif health2 == "invalid":
            a["park_reason"] = "invalid"
            a["parked_until"] = None
            a["last_error"] = usage2.get("error", "invalid")
            log_event("patrol", f"{a['id']} token invalid while parked; needs re-auth")
            changed = True
        _ = due
    save_pool(pool)
    if not changed:
        print("patrol: all quiet")


def cmd_status_json(pool):
    out = {"active": pool["active"], "accounts": []}
    for a in sorted(pool["accounts"], key=lambda x: x["priority"]):
        health, usage = usage_health(pool, a["id"])
        entry = {
            "id": a["id"],
            "account": mask(a["account_id"]),
            "priority": a["priority"],
            "state": a["state"],
            "is_active": a["id"] == pool["active"],
            "park_reason": a.get("park_reason"),
            "parked_until": a.get("parked_until"),
            "health": health,
        }
        if health in ("ok", "capped"):
            entry["plan"] = usage.get("plan_type")
            entry["primary"] = usage.get("primary")
            entry["secondary"] = usage.get("secondary")
        out["accounts"].append(entry)
    print(json.dumps(out))

def cmd_log(n):
    try:
        lines = open(LOG_PATH).read().splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines[-n:]:
        print(line)


# ---------- add / remove accounts ----------

def post_json(url, payload, timeout=25):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": DEVICE_UA}, method="POST")
    try:
        with opener().open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"_err": f"{type(e).__name__}: {str(e)[:160]}"}


def post_form(url, payload, timeout=30):
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": DEVICE_UA}, method="POST")
    try:
        with opener().open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"_err": f"{type(e).__name__}: {str(e)[:160]}"}


def jwt_claims(id_token):
    try:
        import base64
        parts = (id_token or "").split(".")
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}


def cmd_add_begin(pool):
    st, body = post_json(DEVICE_USERCODE_URL, {"client_id": CLIENT_ID})
    if st != 200 or not body.get("user_code") \
            or not body.get("device_auth_id"):
        print(f"add-begin FAILED: HTTP {st} {json.dumps(body)[:200]}")
        return
    save_json(PENDING_ADD, {
        "device_auth_id": body["device_auth_id"],
        "user_code": body["user_code"],
        "created_at": now_ts(),
    })
    log_event("add", "device code issued; waiting for the user to "
                     "authorize a new account")
    print("PENDING AUTHORIZATION")
    print(f"Ask the user to open {DEVICE_VERIFY_URL} and enter this code:")
    print(f"USER_CODE: {body['user_code']}")
    print("The code expires in about 15 minutes. When the user confirms "
          "they have authorized it, call pool_add_finish.")


def cmd_add_finish(pool):
    if not os.path.exists(PENDING_ADD):
        print("no pending add; call pool_add_begin first")
        return
    pending = load_json(PENDING_ADD)
    if now_ts() - pending.get("created_at", 0) > 20 * 60:
        os.unlink(PENDING_ADD)
        print("the pending code expired (>20 min); call pool_add_begin "
              "again")
        return
    st, body = post_json(DEVICE_TOKEN_URL, {
        "device_auth_id": pending["device_auth_id"],
        "user_code": pending["user_code"],
        "client_id": CLIENT_ID,
    })
    auth_code = body.get("authorization_code") \
        if isinstance(body, dict) else None
    if st != 200 or not auth_code:
        err = body.get("error") if isinstance(body, dict) else None
        code = err.get("code") if isinstance(err, dict) else err
        print(f"NOT AUTHORIZED YET (HTTP {st}, {code or 'pending'}). "
              "Ask the user to complete the authorization, then call "
              "pool_add_finish again.")
        return
    form = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": CLIENT_ID,
        "redirect_uri": "https://auth.openai.com/deviceauth/callback",
    }
    verifier = body.get("code_verifier") or body.get("verifier")
    if verifier:
        form["code_verifier"] = verifier
    st, tok = post_form(TOKEN_URL, form)
    if st != 200 or not tok.get("access_token"):
        print(f"add-finish FAILED at token exchange: HTTP {st} "
              f"{json.dumps(tok)[:200]}")
        return
    claims = jwt_claims(tok.get("id_token"))
    account_id = claims.get("chatgpt_account_id")
    if not account_id and isinstance(
            claims.get("https://api.openai.com/auth"), dict):
        account_id = claims["https://api.openai.com/auth"].get(
            "chatgpt_account_id")
    email = claims.get("email")
    if not account_id:
        print("add-finish FAILED: could not read account_id from "
              "id_token")
        return
    for a in pool["accounts"]:
        if a["account_id"] == account_id:
            os.unlink(PENDING_ADD)
            print(f"ALREADY IN POOL: this account is {a['id']} "
                  f"({mask(account_id)}); nothing added")
            return
    expires_at = datetime.fromtimestamp(
        now_ts() + int(tok.get("expires_in", 3600)), timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    token_data = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "id_token": tok.get("id_token"),
        "expires_at": expires_at,
        "account_id": account_id,
    }
    auth_data = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": tok.get("id_token"),
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token", ""),
            "oauth_provenance": {"issuer": "https://auth.openai.com",
                                 "client_id": CLIENT_ID},
            "login_owner_id": account_id,
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000000Z"),
    }
    with Lock():
        pool = load_pool()
        nums = [int(a["id"][4:]) for a in pool["accounts"]
                if a["id"].startswith("acct") and a["id"][4:].isdigit()]
        new_id = f"acct{(max(nums) + 1) if nums else 1}"
        prio = max([a["priority"] for a in pool["accounts"]],
                   default=0) + 1
        auth_path, tokens_path = acct_paths(new_id)
        os.makedirs(os.path.dirname(auth_path), exist_ok=True)
        save_json(auth_path, auth_data)
        save_json(tokens_path, token_data)
        pool["accounts"].append({
            "id": new_id, "account_id": account_id, "priority": prio,
            "state": "standby", "parked_until": None,
            "park_reason": None, "last_error": None,
            "last_check": now_iso(), "grant_shared": False,
        })
        save_pool(pool)
    os.unlink(PENDING_ADD)
    log_event("add", f"{new_id} added ({email or mask(account_id)}), "
                     f"standby, prio {prio}")
    print(f"ADDED: {new_id} account={mask(account_id)} "
          f"email={email or 'unknown'} state=standby priority={prio}")
    health, usage = usage_health(pool, new_id)
    if health in ("ok", "capped"):
        print(f"quota check: plan={usage['plan_type']} "
              f"health={health} week={usage['secondary']['used']}%")
    else:
        print(f"quota check: health={health} (account added anyway)")


def cmd_remove(pool, acct_id):
    a = account(pool, acct_id)
    if not a:
        print(f"unknown account {acct_id}")
        return
    if acct_id == pool["active"]:
        print(f"REFUSED: {acct_id} is the ACTIVE account. Switch to "
              "another account first, then remove it.")
        return
    with Lock():
        pool = load_pool()
        pool["accounts"] = [x for x in pool["accounts"]
                            if x["id"] != acct_id]
        save_pool(pool)
        d = os.path.join(ACCOUNTS_DIR, acct_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    log_event("remove", f"{acct_id} ({mask(a['account_id'])}) removed; "
                        "stored credentials deleted")
    print(f"REMOVED: {acct_id} ({mask(a['account_id'])}); its stored "
          "credentials on the Pi were deleted")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "init":
        with Lock():
            cmd_init()
        return
    pool = load_pool()
    if cmd == "status":
        cmd_status(pool)
    elif cmd == "usage":
        cmd_usage(pool, args[1] if len(args) > 1 else None)
    elif cmd == "refresh":
        with Lock():
            cmd_refresh(pool, args[1] if len(args) > 1 else "all")
    elif cmd == "switch":
        cmd_switch(pool, args[1], "--force" in args)
    elif cmd == "park":
        hours = float(args[2]) if len(args) > 2 else None
        reason = args[3] if len(args) > 3 else "manual"
        cmd_park(pool, args[1], hours, reason)
    elif cmd == "unpark":
        cmd_unpark(pool, args[1])
    elif cmd == "patrol":
        cmd_patrol(pool)
    elif cmd == "add-begin":
        cmd_add_begin(pool)
    elif cmd == "add-finish":
        cmd_add_finish(pool)
    elif cmd == "remove":
        cmd_remove(pool, args[1])
    elif cmd == "status-json":
        cmd_status_json(pool)
    elif cmd == "log":
        cmd_log(int(args[1]) if len(args) > 1 else 20)
    else:
        print(f"unknown command {cmd}")


if __name__ == "__main__":
    main()

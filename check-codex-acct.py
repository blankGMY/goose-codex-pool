import base64
import json
import os

path = os.path.expanduser(r"~\.codex\auth.json")
d = json.load(open(path, encoding="utf-8"))
tok = d.get("tokens", {})
print("file account_id:", (tok.get("account_id") or "")[:8])
idt = tok.get("id_token") or ""
if idt.count(".") == 2:
    payload = idt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    print("email:", claims.get("email"))
    print("name:", claims.get("name"))
    auth_claims = claims.get("https://api.openai.com/auth", {})
    print("chatgpt_account_id:", (auth_claims.get("chatgpt_account_id") or "")[:8])
    print("plan:", auth_claims.get("chatgpt_plan_type"))
else:
    print("no id_token in file")
print("last_refresh:", d.get("last_refresh"))

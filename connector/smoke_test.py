#!/usr/bin/env python3
"""End to end check of a running cw connector, with no browser.

    smoke_test.py https://cw.example.com [--via http://127.0.0.1:8787] [--spawn WORKSPACE]

Registers a client, asks for authorization, approves the code with the local
approve command (so run it on the machine that hosts the connector, with a
pairing window open and the dialog off: cw_connector.py pair --no-dialog),
exchanges the code with PKCE, refreshes once, then calls every tool.
--via sends the requests to a local address while keeping the public host name.
--spawn starts one harmless test session and prints its number.
The test client and its tokens are dropped at the end.
"""
import base64, hashlib, json, os, re, secrets, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

args = sys.argv[1:]
public = args[0].rstrip("/")
via = args[args.index("--via") + 1].rstrip("/") if "--via" in args else public
spawn_ws = args[args.index("--spawn") + 1] if "--spawn" in args else ""
HOST = urllib.parse.urlparse(public).netloc
HERE = os.path.dirname(os.path.abspath(__file__))
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoRedirect)


def http(method, path, data=None, headers=None, form=False):
    url = path.replace(public, via, 1) if path.startswith("http") else via + path
    h = {"Host": HOST, "User-Agent": "cw-connector-smoke-test", **(headers or {})}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        r = opener.open(urllib.request.Request(url, data=body, headers=h, method=method), timeout=240)
    except urllib.error.HTTPError as e:
        r = e
    return r.status, dict(r.headers), r.read().decode("utf-8", "replace")


def step(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


s, _, b = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
step("no token is refused", s == 401, f"status {s}")
s, _, b = http("POST", "/register", {"client_name": "Bad Client", "redirect_uris": ["https://example.org/cb"],
                                     "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none"})
step("foreign redirect is refused at registration", s == 400, f"status {s}")
s, _, b = http("POST", "/register", {"client_name": "Smoke Test", "redirect_uris": [CALLBACK], "response_types": ["code"],
                                     "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none", "scope": "cw"})
step("client registers", s in (200, 201), f"status {s} {b[:120] if s >= 300 else ''}")
client = json.loads(b)
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(12)
q = urllib.parse.urlencode({"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK, "scope": "cw",
                            "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": public + "/mcp"})
s, h, b = http("GET", "/authorize?" + q)
loc = h.get("location") or h.get("Location") or ""
step("authorize hands over to the approval page", s in (302, 303, 307) and "/approve?rid=" in loc, f"status {s}")
rid = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["rid"][0]
s, _, page = http("GET", "/approve?rid=" + rid)
m = re.search(r'class="code">([A-Z0-9-]+)<', page)
step("approval page shows a code", bool(m), "" if m else re.sub(r"<[^>]+>", " ", page)[-160:].strip())
s, _, b = http("GET", "/approve/status?rid=" + rid)
step("nothing is granted before approval", json.loads(b)["status"] == "waiting")
subprocess.run([sys.executable, os.path.join(HERE, "cw_connector.py"), "approve", m.group(1)], check=True, capture_output=True)
s, _, b = http("GET", "/approve/status?rid=" + rid)
d = json.loads(b)
step("local approval releases the redirect", d["status"] == "approved" and d["redirect"].startswith(CALLBACK))
back = urllib.parse.parse_qs(urllib.parse.urlparse(d["redirect"]).query)
step("state comes back unchanged", back["state"][0] == state)
s, _, b = http("POST", "/token", {"grant_type": "authorization_code", "code": back["code"][0], "redirect_uri": CALLBACK,
                                  "client_id": client["client_id"], "code_verifier": "wrong" + verifier}, form=True)
step("wrong PKCE verifier is refused", s == 400, f"status {s}")
s, _, page = http("GET", "/authorize?" + q)
s, _, page = http("GET", (dict(_).get("location") or dict(_).get("Location") or "/approve?rid=x"))
step("pairing closed itself after one approval", "Pairing is closed" in page)

# The wrong verifier burned the first code, as it should. Open a fresh window for the real exchange.
subprocess.run([sys.executable, os.path.join(HERE, "cw_connector.py"), "pair", "5", "--no-dialog"], check=True, capture_output=True)
s, h, b = http("GET", "/authorize?" + q)
rid = urllib.parse.parse_qs(urllib.parse.urlparse(h.get("location") or h.get("Location")).query)["rid"][0]
_, _, page = http("GET", "/approve?rid=" + rid)
subprocess.run([sys.executable, os.path.join(HERE, "cw_connector.py"), "approve", re.search(r'class="code">([A-Z0-9-]+)<', page).group(1)], check=True, capture_output=True)
d = json.loads(http("GET", "/approve/status?rid=" + rid)[2])
code = urllib.parse.parse_qs(urllib.parse.urlparse(d["redirect"]).query)["code"][0]
s, _, b = http("POST", "/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                                  "client_id": client["client_id"], "code_verifier": verifier}, form=True)
step("code exchanges for tokens", s == 200, f"status {s} {b[:160] if s != 200 else ''}")
tok = json.loads(b)
s, _, b = http("POST", "/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client["client_id"]}, form=True)
step("refresh token works and rotates", s == 200 and json.loads(b)["refresh_token"] != tok["refresh_token"], f"status {s}")
tok = json.loads(b)
AUTH = {"Authorization": "Bearer " + tok["access_token"], "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
ids = iter(range(1, 999))


def rpc(method, params=None):
    s, _, b = http("POST", "/mcp", {"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params or {}}, headers=AUTH)
    if s != 200:
        step(method, False, f"status {s} {b[:200]}")
    if b.lstrip().startswith("event:") or b.lstrip().startswith("data:"):
        b = [l[5:] for l in b.splitlines() if l.startswith("data:")][-1]
    return json.loads(b)


def call(name, **arguments):
    r = rpc("tools/call", {"name": name, "arguments": arguments})
    text = "\n".join(c.get("text", "") for c in r.get("result", {}).get("content", []))
    ok = "result" in r and not r["result"].get("isError")
    print(f"{'PASS' if ok else 'FAIL'}  tool {name}")
    for line in (text or json.dumps(r)[:300]).splitlines()[:6]:
        print("        " + line[:150])
    if not ok:
        sys.exit(1)
    return text


r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke-test", "version": "1"}})
step("initialize", "result" in r, r.get("result", {}).get("serverInfo", {}).get("name", ""))
tools = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
step("tools are listed", len(tools) >= 9, ", ".join(tools))
step("no kill, shell or write tool exists", not any(re.search(r"kill|shell|exec|write|delete|remove", t) for t in tools))
call("list_sessions")
call("peek_session", session="0", lines=8)
call("route_task", task="Draft a reply to a parent asking about the mentorship programme")
call("list_local", scope="transcripts", limit=3)
call("list_local", scope="memory")
found = call("search_local", query="voice relay", scope="repo")
call("read_local", path="~/.claude/../.ssh/id_ed25519")
call("read_local", path=re.search(r"(~\S+README\.md)", found).group(1) if "README.md" in found else "~/nope", max_chars=600)
call("master_last_reply")
call("ask_master", message="Connector smoke test. Reply with exactly: connector works.", wait_seconds=60, spoken=False)
if spawn_ws:
    out = call("spawn_session", workspace=spawn_ws, task="Connector smoke test session. Reply with the single word ready and then do nothing else. Do not run any tool.")
    sid = re.search(r"session (\d+)", out)
    if sid:
        time.sleep(12)
        call("send_to_session", session=sid.group(1), text="Smoke test follow up. Reply with the single word received.")
        time.sleep(10)
        call("peek_session", session=sid.group(1), lines=14)
        print("TEST SESSION ID", sid.group(1))
subprocess.run([sys.executable, os.path.join(HERE, "cw_connector.py"), "forget", client["client_id"]], check=True, capture_output=True)
time.sleep(1.1)
s3, _, _ = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=AUTH)
step("a forgotten client's token is refused", s3 == 401, f"status {s3}")
print("CLIENT", client["client_id"])

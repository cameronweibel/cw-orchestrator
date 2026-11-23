#!/usr/bin/env python3
"""cw connector: a remote MCP server that lets a Claude app drive cw by voice or text.

    cw_connector.py serve              run the server (launchd does this)
    cw_connector.py pair [minutes]     open a pairing window, default 15 minutes
    cw_connector.py pair --no-dialog   same, approval by the approve command only
    cw_connector.py approve CODE       approve the pairing request showing CODE
    cw_connector.py deny CODE          refuse it
    cw_connector.py status             pairing window, clients and live tokens
    cw_connector.py forget CLIENT      drop one client (id or its first characters) and its tokens
    cw_connector.py revoke             drop every client and token

It speaks streamable HTTP MCP on 127.0.0.1 and is meant to sit behind an HTTPS
tunnel. Access is OAuth 2.1 with dynamic client registration, as Claude custom
connectors expect. There is no password. A new client is accepted only while a
pairing window is open on this machine, and only after the owner approves the
code shown in the browser, in a dialog on this machine or with the approve
command. One approval closes the window.

Tools: list, peek, send to and spawn cw sessions, ask the master session, and
search and read a fixed set of local folders. There is no kill, no shell and no
file write.

Everything personal lives in config.json next to the state, never in this file.
"""
import asyncio, fnmatch, glob, hashlib, html, ipaddress, json, os, re, secrets, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
DIR = Path(os.environ.get("CW_CONNECTOR_DIR", HOME / ".claude/cw-connector"))
CONFIG, STATE, PAIRING = DIR / "config.json", DIR / "state.json", DIR / "pairing.json"
APPROVALS, AUDIT = DIR / "approvals", DIR / "audit.log"
BIN = HOME / ".claude/bin"
SCOPE = "cw"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

DEFAULTS = {
    "public_url": "https://cw.example.com",
    "port": 8787,
    "redirect_allow": ["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"],
    "allow_loopback_redirect": False,
    "ip_allow": ["160.79.104.0/21"],
    "tmux_socket": "default",
    "tmux_session": "ws",
    "master_window": "0",
    "master_target": "",
    "master_sid": "",
    "machine": "Mac",
    "access_ttl": 3600,
    "refresh_ttl_days": 60,
    "spawns_per_hour": 10,
    "roots": {
        "out": ["~/workspaces/*/out"],
        "memory": ["~/.claude/projects/*/memory"],
        "transcripts": ["~/.claude/projects"],
        "repo": ["~/cw-orchestrator"],
    },
}
DENY_NAMES = [".env*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*secret*", "*credential*",
              "*token*", "auth.json", "state.json", ".netrc", "*.sqlite*", "*.db", "*.keychain*", "cert.*"]
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist", ".next", "build", ".cache"}
REDACT = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|ya29\.[A-Za-z0-9_.-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    cfg["public_url"] = cfg["public_url"].rstrip("/")
    return cfg


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.replace(tmp, path)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"clients": {}, "access": {}, "refresh": {}}


def save_state(state):
    write_private(STATE, json.dumps(state, indent=1))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def pairing_window():
    try:
        w = json.loads(PAIRING.read_text())
    except (OSError, ValueError):
        return None
    return w if w.get("until", 0) > time.time() else None


def audit(event, **fields):
    line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}, ensure_ascii=False)
    with open(AUDIT, "a") as f:
        f.write(line[:2000] + "\n")


# ----------------------------------------------------------------------------- command line

def cli(argv):
    cmd = argv[0] if argv else "serve"
    DIR.mkdir(parents=True, exist_ok=True)
    if cmd == "pair":
        rest = [a for a in argv[1:] if a != "--no-dialog"]
        minutes = int(rest[0]) if rest else 15
        write_private(PAIRING, json.dumps({"until": time.time() + minutes * 60, "dialog": "--no-dialog" not in argv}))
        print(f"Pairing is open for {minutes} minutes. Add the connector now, then approve the code it shows.")
    elif cmd in ("approve", "deny") and len(argv) == 2:
        code = re.sub(r"[^A-Z0-9]", "", argv[1].upper())
        APPROVALS.mkdir(mode=0o700, exist_ok=True)
        write_private(APPROVALS / code, cmd)
        print(f"{cmd} recorded for {code}. The waiting browser page picks it up within two seconds.")
    elif cmd == "status":
        w, st = pairing_window(), load_state()
        now = time.time()
        print("pairing:", f"open for {int((w['until'] - now) / 60) + 1} more minutes" if w else "closed")
        for cid, c in st["clients"].items():
            live = sum(1 for t in st["refresh"].values() if t["client_id"] == cid and t["expires_at"] > now)
            print(f"client {cid[:8]}  {c['info'].get('client_name') or '?'}  approved={c.get('approved')}  live refresh tokens={live}")
        if not st["clients"]:
            print("no clients")
    elif cmd == "forget" and len(argv) == 2 and len(argv[1]) >= 6:
        st = load_state()
        gone = [c for c in st["clients"] if c.startswith(argv[1])]
        for c in gone:
            del st["clients"][c]
        for table in ("access", "refresh"):
            st[table] = {k: t for k, t in st[table].items() if t["client_id"] not in gone}
        save_state(st)
        print(f"Dropped {len(gone)} client(s) and their tokens.")
    elif cmd == "revoke":
        save_state({"clients": {}, "access": {}, "refresh": {}})
        PAIRING.unlink(missing_ok=True)
        print("All clients and tokens are gone.")
    elif cmd == "serve":
        serve()
    else:
        print(__doc__)
        sys.exit(1)


# ----------------------------------------------------------------------------- server

def serve():
    import uvicorn
    from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizeError, RefreshToken,
                                          RegistrationError, construct_redirect_uri)
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
    from mcp.types import ToolAnnotations
    from starlette.responses import HTMLResponse, JSONResponse

    cfg = load_config()
    public = cfg["public_url"]
    host = public.split("://", 1)[1]
    port = int(cfg["port"])
    nets = [ipaddress.ip_network(n) for n in cfg["ip_allow"]]
    state = load_state()
    seen = {"mtime": STATE.stat().st_mtime if STATE.exists() else 0}

    def sync():
        """Pick up changes the command line made to the state file (forget, revoke)."""
        m = STATE.stat().st_mtime if STATE.exists() else 0
        if m != seen["mtime"]:
            fresh = load_state()
            for k in ("clients", "access", "refresh"):
                state[k] = fresh.get(k, {})
            seen["mtime"] = m

    def save():
        save_state(state)
        seen["mtime"] = STATE.stat().st_mtime

    # Sessions that cw spawns inherit this PATH, so it has to be the login shell's, not launchd's bare one.
    base_path = f"{BIN}:{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    try:
        shell = os.environ.get("SHELL") or next(s for s in ("/bin/zsh", "/bin/bash", "/bin/sh") if os.path.exists(s))
        got = subprocess.run([shell, "-lic", "printf '\\n__PATH__%s' \"$PATH\""], capture_output=True, text=True,
                             timeout=20, stdin=subprocess.DEVNULL).stdout
        login_path = got.rsplit("__PATH__", 1)[1].strip() if "__PATH__" in got else ""
    except (OSError, subprocess.TimeoutExpired):
        login_path = ""
    env = dict(os.environ, PATH=f"{base_path}:{login_path}" if login_path else base_path)
    tmux_base = ["tmux", "-L", cfg["tmux_socket"]] if cfg["tmux_socket"] else ["tmux"]
    workspaces = []
    try:
        ws = json.loads((HOME / ".claude/workspaces.json").read_text())["workspaces"]
        workspaces = [w if isinstance(w, str) else w.get("name") for w in ws]
    except (OSError, ValueError, KeyError):
        pass

    def redirect_ok(uri):
        if uri in cfg["redirect_allow"]:
            return True
        return bool(cfg["allow_loopback_redirect"] and re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?/", uri))

    class Provider:
        def __init__(self):
            self.pending, self.codes = {}, {}

        async def get_client(self, client_id):
            sync()
            c = state["clients"].get(client_id)
            return OAuthClientInformationFull.model_validate(c["info"]) if c else None

        async def register_client(self, info):
            sync()
            uris = [str(u) for u in info.redirect_uris or []]
            if not uris or not all(redirect_ok(u) for u in uris):
                audit("register_refused", redirect_uris=uris)
                raise RegistrationError("invalid_redirect_uri", "This server accepts Claude's callback only.")
            now = time.time()
            for cid in [k for k, c in state["clients"].items() if not c.get("approved") and now - c["created"] > 3600]:
                del state["clients"][cid]
            if len(state["clients"]) >= 40:
                raise RegistrationError("invalid_client_metadata", "Too many clients are registered.")
            state["clients"][info.client_id] = {"info": info.model_dump(mode="json"), "created": now, "approved": False}
            save()
            audit("register", client=info.client_id[:8], name=info.client_name)

        async def authorize(self, client, params):
            now = time.time()
            for rid in [r for r, p in self.pending.items() if now - p["created"] > 300]:
                del self.pending[rid]
            rid = secrets.token_urlsafe(24)
            window = pairing_window()
            waiting = sum(1 for p in self.pending.values() if p["status"] == "waiting")
            if not window or waiting >= 3:
                self.pending[rid] = {"created": now, "status": "closed" if not window else "busy"}
                audit("authorize_refused", reason="closed" if not window else "busy", client=client.client_id[:8])
                return f"{public}/approve?rid={rid}"
            raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            self.pending[rid] = {"created": now, "status": "waiting", "code": raw, "client_id": client.client_id,
                                 "name": re.sub(r"[^A-Za-z0-9 ._-]", "", client.client_name or "A client")[:40],
                                 "params": params}
            audit("authorize", client=client.client_id[:8], code=raw)
            if window.get("dialog", True):
                asyncio.create_task(self.dialog(rid))
            return f"{public}/approve?rid={rid}"

        async def dialog(self, rid):
            p = self.pending[rid]
            text = (f"Allow {p['name']} to control your cw sessions?\\n\\nCode: {p['code'][:4]}-{p['code'][4:]}\\n\\n"
                    "Allow only if you are adding the connector right now and this code matches the one in your browser.")
            script = (f'display dialog "{text}" with title "cw connector" buttons {{"Deny", "Allow"}} '
                      'default button "Deny" giving up after 240')
            try:
                proc = await asyncio.create_subprocess_exec("osascript", "-e", "tell me to activate", "-e", script,
                                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, _ = await proc.communicate()
            except OSError:
                return
            if p["status"] == "waiting" and b"button returned:Allow" in out:
                p["decision"] = "approve"
            elif p["status"] == "waiting" and b"button returned:Deny" in out:
                p["decision"] = "deny"

        def settle(self, rid):
            """Move a waiting request on if a decision has arrived. Returns the request."""
            p = self.pending.get(rid)
            if not p or p["status"] != "waiting":
                return p
            if time.time() - p["created"] > 300:
                p["status"] = "expired"
                return p
            mark = APPROVALS / p["code"]
            decision = p.get("decision")
            if mark.exists():
                decision = mark.read_text().strip()
                mark.unlink()
            if decision == "deny":
                p["status"] = "denied"
                audit("denied", code=p["code"])
            elif decision == "approve":
                params = p["params"]
                code = secrets.token_urlsafe(32)
                self.codes[code] = AuthorizationCode(
                    code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + 300, client_id=p["client_id"],
                    code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=params.resource, subject="owner")
                p["redirect"] = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
                p["status"] = "approved"
                if p["client_id"] in state["clients"]:
                    state["clients"][p["client_id"]]["approved"] = True
                    save()
                PAIRING.unlink(missing_ok=True)
                audit("approved", code=p["code"], client=p["client_id"][:8])
            return p

        async def load_authorization_code(self, client, authorization_code):
            c = self.codes.get(authorization_code)
            return c if c and c.client_id == client.client_id and c.expires_at > time.time() else None

        def issue(self, client_id, scopes, resource):
            sync()
            now = int(time.time())
            access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
            for table in ("access", "refresh"):
                for k in [k for k, t in state[table].items() if t["expires_at"] < now]:
                    del state[table][k]
            state["access"][digest(access)] = {"client_id": client_id, "scopes": scopes, "resource": resource,
                                               "expires_at": now + int(cfg["access_ttl"])}
            state["refresh"][digest(refresh)] = {"client_id": client_id, "scopes": scopes, "resource": resource,
                                                 "expires_at": now + int(cfg["refresh_ttl_days"]) * 86400}
            save()
            return OAuthToken(access_token=access, expires_in=int(cfg["access_ttl"]), scope=" ".join(scopes),
                              refresh_token=refresh)

        async def exchange_authorization_code(self, client, authorization_code):
            self.codes.pop(authorization_code.code, None)
            audit("token_issued", client=client.client_id[:8])
            return self.issue(client.client_id, authorization_code.scopes, authorization_code.resource)

        async def load_refresh_token(self, client, refresh_token):
            sync()
            t = state["refresh"].get(digest(refresh_token))
            if not t or t["client_id"] != client.client_id or t["expires_at"] < time.time():
                return None
            return RefreshToken(token=refresh_token, client_id=t["client_id"], scopes=t["scopes"],
                                expires_at=t["expires_at"], resource=t.get("resource"), subject="owner")

        async def exchange_refresh_token(self, client, refresh_token, scopes):
            # Rotation with a short grace: a retried refresh after a dropped reply must not lock the app out.
            old = state["refresh"].get(digest(refresh_token.token))
            if old:
                old["expires_at"] = min(old["expires_at"], int(time.time()) + 600)
            return self.issue(client.client_id, refresh_token.scopes, refresh_token.resource)

        async def load_access_token(self, token):
            sync()
            t = state["access"].get(digest(token))
            if not t or t["expires_at"] < time.time() or t["client_id"] not in state["clients"]:
                return None
            return AccessToken(token=token, client_id=t["client_id"], scopes=t["scopes"], expires_at=t["expires_at"],
                               resource=t.get("resource"), subject="owner")

        async def revoke_token(self, token):
            for table in ("access", "refresh"):
                state[table].pop(digest(token.token), None)
            save()

        async def exchange_identity_assertion(self, client, params):
            raise NotImplementedError

    provider = Provider()
    mcp = MCPServer(
        name="cw",
        instructions=(
            f"Controls the owner's cw sessions on their {cfg['machine']}. Each session is a Claude Code agent working in one "
            "workspace. The master session knows about every other session and is the best place for open "
            "questions: use ask_master for those. Use list_sessions and peek_session to see what is running, "
            "send_to_session to instruct one session, spawn_session to start new work. search_local and read_local "
            "read past outputs, memory notes and session transcripts. Nothing here can kill a session, run a shell "
            "command or change a file. When speaking, keep answers short and do not read out ids, paths or tables "
            "unless asked."),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=public, resource_server_url=f"{public}/mcp", required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True), validate_token_resource=False),
    )

    # ------------------------------------------------------------------ approval page

    PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>cw connector</title><style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1c2330;--muted:#5b6472;--line:#d7dbe2}
@media(prefers-color-scheme:dark){:root{--bg:#12161d;--card:#1b212b;--ink:#eef1f5;--muted:#9aa4b2;--line:#2c3440}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;line-height:1.5;padding:16px}
main{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;max-width:420px;width:100%}
h1{font-size:20px;margin:0 0 10px}p{margin:0 0 12px;color:var(--muted);font-size:15px}
.code{font:600 30px/1.2 "SF Mono",ui-monospace,Menlo,Consolas,monospace;letter-spacing:3px;color:var(--ink);
margin:18px 0;padding:14px;border:1px dashed var(--line);border-radius:10px;text-align:center}
</style></head><body><main>__BODY__</main>__SCRIPT__</body></html>"""
    POLL = """<script>
const rid=new URLSearchParams(location.search).get("rid");
async function tick(){
  try{
    const r=await fetch("/approve/status?rid="+encodeURIComponent(rid),{cache:"no-store"});
    const d=await r.json();
    if(d.status==="approved"){location.replace(d.redirect);return}
    if(d.status!=="waiting"){document.querySelector("main").innerHTML="<h1>"+d.title+"</h1><p>"+d.text+"</p>";return}
  }catch(e){}
  setTimeout(tick,1500);
}
tick();
</script>"""
    MESSAGES = {
        "closed": ("Pairing is closed", f"Open a pairing window on the {cfg['machine']} with the command cw-connector pair, then add the connector again."),
        "busy": ("Too many requests are waiting", "Try again in a few minutes."),
        "denied": ("The request was refused", "Nothing was connected."),
        "expired": ("The request ran out of time", "Add the connector again to get a new code."),
        "gone": ("This page is no longer valid", "Add the connector again to get a new code."),
        "approved": ("Connected", "You can close this page."),
    }
    HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"}

    @mcp.custom_route("/approve", methods=["GET"])
    async def approve_page(request):
        p = provider.settle(request.query_params.get("rid", ""))
        status = p["status"] if p else "gone"
        if status == "waiting":
            code = p["code"][:4] + "-" + p["code"][4:]
            machine = html.escape(cfg["machine"])
            how = (f"A dialog on the {machine} shows a code. Allow it only if it matches this one."
                   if (pairing_window() or {}).get("dialog", True) and sys.platform == "darwin"
                   else f"On the {machine}, run cw-connector approve with this code.")
            body = (f"<h1>Approve this on your {machine}</h1><p>{how}</p>"
                    f'<div class="code">{html.escape(code)}</div><p>This page continues by itself once you allow it. The code is valid for five minutes.</p>')
            return HTMLResponse(PAGE.replace("__BODY__", body).replace("__SCRIPT__", POLL), headers=HEADERS)
        title, text = MESSAGES[status]
        return HTMLResponse(PAGE.replace("__BODY__", f"<h1>{title}</h1><p>{text}</p>").replace("__SCRIPT__", ""), headers=HEADERS)

    @mcp.custom_route("/approve/status", methods=["GET"])
    async def approve_status(request):
        p = provider.settle(request.query_params.get("rid", ""))
        status = p["status"] if p else "gone"
        out = {"status": status}
        if status == "approved":
            out["redirect"] = p.pop("redirect", None)
            if not out["redirect"]:
                out["status"] = status = "gone"
        if status not in ("waiting", "approved"):
            out["title"], out["text"] = MESSAGES[status]
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/", methods=["GET"])
    async def root(request):
        return JSONResponse({"service": "cw connector", "mcp": f"{public}/mcp"})

    # ------------------------------------------------------------------ helpers for tools

    def run(cmd, timeout=30, extra_env=None, limit=12000):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=dict(env, **(extra_env or {})),
                               stdin=subprocess.DEVNULL, cwd=str(HOME))
        except subprocess.TimeoutExpired:
            return f"Timed out after {timeout} seconds."
        out = (r.stdout + (("\n" + r.stderr) if r.returncode and r.stderr else "")).strip()
        out = REDACT.sub("[redacted]", out)
        return out[:limit] + ("\n[cut]" if len(out) > limit else "") if out else f"(no output, exit {r.returncode})"

    def session_arg(session):
        s = str(session).strip()
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,80}", s):
            raise ValueError("A session is its number from list_sessions, or its exact name.")
        return s

    def window_ids():
        r = subprocess.run(tmux_base + ["list-windows", "-t", cfg["tmux_session"], "-F", "#{window_index}"],
                           capture_output=True, text=True, env=env)
        return set(r.stdout.split())

    def roots(scope):
        out = []
        for name, patterns in cfg["roots"].items():
            if scope in ("all", name):
                for pat in patterns:
                    for hit in glob.glob(os.path.expanduser(pat)):
                        real = os.path.realpath(hit)
                        if os.path.isdir(real):
                            out.append((name, real))
        return out

    def denied(name):
        low = name.lower()
        return any(fnmatch.fnmatch(low, pat) for pat in DENY_NAMES)

    def resolve(path):
        """A real path inside one of the allowed roots, or an error. Symlinks that leave the roots are refused."""
        real = os.path.realpath(os.path.expanduser(str(path).strip()))
        for name, root in roots("all"):
            if real == root or real.startswith(root + os.sep):
                rel = os.path.relpath(real, root)
                if any(denied(part) for part in Path(rel).parts if part != "."):
                    raise ValueError("That file is on the list of names this server never reads.")
                if name == "transcripts" and os.path.isfile(real) and not real.endswith((".jsonl", ".md", ".json", ".txt")):
                    raise ValueError("Only transcripts and notes are readable there.")
                return name, real
        raise ValueError("That path is outside the folders this server may read. Use list_local to see them.")

    def short(path):
        return path.replace(str(HOME), "~", 1)

    def turns(path, limit_each=1500):
        """User and assistant text of a Claude Code transcript, oldest first."""
        out = []
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                kind = r.get("type")
                if kind not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta"):
                    continue
                content = (r.get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                else:
                    text = "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
                text = text.strip()
                if not text or text.startswith(("<system-reminder>", "<command-name>", "<local-command")):
                    continue
                who = "USER" if kind == "user" else "CLAUDE"
                out.append(f"{who} ({(r.get('timestamp') or '')[:16]}): {text[:limit_each]}{' [cut]' if len(text) > limit_each else ''}")
        return out

    def first_prompt(path):
        try:
            with open(path, errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 40:
                        break
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("type") == "user" and not r.get("isMeta"):
                        c = (r.get("message") or {}).get("content")
                        text = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
                        text = " ".join(text.split())
                        if text and not text.startswith("<"):
                            return text[:110]
        except OSError:
            pass
        return ""

    def candidates(scope, days, cap=5000):
        cutoff = time.time() - days * 86400 if days else 0
        files = []
        for name, root in roots(scope):
            for base, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not denied(d)]
                for n in names:
                    if denied(n) or (name == "transcripts" and not n.endswith((".jsonl", ".md"))):
                        continue
                    p = os.path.join(base, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if st.st_size > 60_000_000 or st.st_mtime < cutoff:
                        continue
                    files.append((st.st_mtime, p))
        files.sort(reverse=True)
        return files[:cap]

    def rg_binary():
        for p in ("/opt/homebrew/bin/rg", "/usr/local/bin/rg", "/usr/bin/rg"):
            if os.path.exists(p):
                return p
        return os.path.realpath(HOME / ".local/bin/claude")  # Claude Code ships ripgrep, selected by argv[0]

    spawn_times = []
    READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    def tool(name, title, annotations):
        def wrap(fn):
            async def call(**kwargs):
                audit("tool", name=name, args={k: str(v)[:200] for k, v in kwargs.items()})
                try:
                    return await asyncio.to_thread(fn, **kwargs)
                except ValueError as e:
                    return f"Not done: {e}"
            call.__name__, call.__doc__ = fn.__name__, fn.__doc__
            call.__signature__ = __import__("inspect").signature(fn)
            call.__annotations__ = fn.__annotations__
            return mcp.tool(name=name, title=title, annotations=annotations)(call)
        return wrap

    # ------------------------------------------------------------------ tools

    @tool("list_sessions", "List cw sessions", READ)
    def list_sessions() -> str:
        """List every cw session on this machine: its number, workspace, title, whether it is busy, how long since its last message, and its task. The master session may not appear in the list; reach it with ask_master."""
        rows = run([str(BIN / "cw"), "ls"], limit=60000).splitlines()
        body = "\n".join(line[:190] + ("..." if len(line) > 190 else "") for line in rows)
        if len(rows) < 2:
            return f"No worker sessions are running on this {cfg['machine']}. The master session is reached with ask_master."
        return f"{len(rows) - 1} sessions in total, all listed below. Long task texts are shortened.\n{body}"

    @tool("peek_session", "Peek at a session", READ)
    def peek_session(session: str, lines: int = 40) -> str:
        """Show the last lines on one session's screen. session is its number from list_sessions. lines is between 5 and 200."""
        return run([str(BIN / "cw"), "peek", session_arg(session), str(max(5, min(int(lines), 200)))])

    @tool("send_to_session", "Send text to a session", ACT)
    def send_to_session(session: str, text: str) -> str:
        """Type a message into one session and press Enter, as if the owner typed it there. Use it to instruct or answer a specific session. Returns the session's screen a moment later."""
        text = " ".join(str(text).split())
        if not text or len(text) > 4000:
            raise ValueError("The text must be between 1 and 4000 characters.")
        sid = session_arg(session)
        sent = run([str(BIN / "cw"), "send", sid, text], timeout=15)
        time.sleep(2)
        return sent + "\n\nScreen now:\n" + run([str(BIN / "cw"), "peek", sid, "14"])

    @tool("route_task", "Preview routing", READ)
    def route_task(task: str) -> str:
        """Dry run: say which workspace cw would route a task to, and how sure it is. Starts nothing."""
        return run([str(BIN / "cw"), "route", " ".join(str(task).split())[:3000]], timeout=90)

    @tool("spawn_session", "Start a new session", ACT)
    def spawn_session(task: str, workspace: str = "") -> str:
        """Start a new cw session that begins working on task right away. workspace is one of the configured workspaces. Leave it empty to let cw route the task; if routing is unsure you get the options back and nothing starts."""
        task = " ".join(str(task).split())
        if not 3 <= len(task) <= 6000:
            raise ValueError("The task must be between 3 and 6000 characters.")
        now = time.time()
        spawn_times[:] = [t for t in spawn_times if now - t < 3600]
        if len(spawn_times) >= int(cfg["spawns_per_hour"]):
            raise ValueError("The hourly limit for new sessions is reached.")
        ws = str(workspace).strip().lower()
        if ws and ws not in workspaces:
            raise ValueError(f"Unknown workspace. Choose one of: {', '.join(workspaces)}.")
        if not ws:
            routed = run([str(BIN / "cw"), "route", task], timeout=90)
            m = re.search(r"workspace=(\S+).*?confidence=([0-9.]+)", routed)
            if not m or m.group(1) not in workspaces or float(m.group(2)) < 0.7:
                return f"Routing is unsure ({routed.splitlines()[-1][:160] if routed else 'no answer'}). Nothing started. Ask which workspace, then call again with it: {', '.join(workspaces)}."
            ws = m.group(1)
        before = window_ids()
        out = run([str(BIN / "cw"), "-d", ws, task], timeout=60)
        new = sorted(window_ids() - before, key=int)
        spawn_times.append(now)
        if not new:
            return f"cw did not report a new session. Output: {out[:400]}"
        return f"Started session {new[0]} in workspace {ws}. {out.splitlines()[-1][:160]}"

    master_env = {"ASK_MASTER_TMUX": cfg["master_target"] or f"{cfg['tmux_session']}:{cfg['master_window']}",
                  "ASK_MASTER_SOCKET": cfg["tmux_socket"]}
    if cfg["master_sid"]:
        master_env["ASK_MASTER_SID"] = cfg["master_sid"]

    @tool("ask_master", "Ask the master session", ACT)
    def ask_master(message: str, wait_seconds: int = 45, spoken: bool = True) -> str:
        """Send a message to the master session and return its reply. The master sees every session and can check on them, steer them and start new ones, so open questions and multi step requests belong here. If it is still working after wait_seconds (10 to 180), call master_last_reply a little later. Set spoken to false for a detailed written answer."""
        message = " ".join(str(message).split())
        if not message or len(message) > 6000:
            raise ValueError("The message must be between 1 and 6000 characters.")
        wait = max(10, min(int(wait_seconds), 180))
        cmd = [str(BIN / "ask-master"), "--wait", str(wait)] + (["--voice"] if spoken else []) + ["--", message]
        return run(cmd, timeout=wait + 25, extra_env=master_env)

    @tool("master_last_reply", "Master's latest reply", READ)
    def master_last_reply() -> str:
        """The master session's most recent reply, and whether it is still working. Use after ask_master reported that the master needs more time."""
        return run([str(BIN / "ask-master"), "--last"], timeout=20, extra_env=master_env)

    @tool("list_local", "List local data", READ)
    def list_local(scope: str = "transcripts", path: str = "", limit: int = 30) -> str:
        """List readable local data. scope is out (workspace output folders), memory (memory notes), transcripts (past session transcripts, newest first with their opening request) or repo (the cw orchestrator repo). Give path to list one folder instead."""
        limit = max(1, min(int(limit), 100))
        if path:
            _, real = resolve(path)
            if not os.path.isdir(real):
                raise ValueError("That is a file. Use read_local.")
            rows = []
            for e in os.scandir(real):
                if denied(e.name) or e.name in SKIP_DIRS:
                    continue
                st = e.stat()
                rows.append((st.st_mtime, f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}  {'dir ' if e.is_dir() else str(st.st_size).rjust(9)}  {short(e.path)}"))
            return "\n".join(r for _, r in sorted(rows, reverse=True)[:limit]) or "Empty."
        if scope not in cfg["roots"]:
            raise ValueError(f"scope is one of: {', '.join(cfg['roots'])}.")
        if scope == "transcripts":
            rows = []
            for mtime, p in [c for c in candidates("transcripts", 0) if c[1].endswith(".jsonl")][:limit]:
                rows.append(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}  {short(p)}\n    {first_prompt(p)}")
            return "\n".join(rows) or "No transcripts."
        return "\n".join(short(r) for _, r in roots(scope)) or "Nothing is configured for that scope."

    @tool("search_local", "Search local data", READ)
    def search_local(query: str, scope: str = "all", days: int = 0, max_files: int = 15) -> str:
        """Search file contents, case insensitive, for an exact phrase. scope is all, out, memory, transcripts or repo. days limits the search to files changed in the last N days (0 means no limit; set it for transcripts to keep the search fast). Returns matching files, newest first, with short excerpts."""
        query = str(query).strip()
        if not 2 <= len(query) <= 200:
            raise ValueError("The query must be between 2 and 200 characters.")
        if scope != "all" and scope not in cfg["roots"]:
            raise ValueError(f"scope is one of: all, {', '.join(cfg['roots'])}.")
        files = candidates(scope, max(0, int(days)))
        if not files:
            return "No files in that scope."
        mtimes = {p: m for m, p in files}
        pattern = ".{0,110}" + re.escape(query) + ".{0,110}"
        binary, hits, deadline = rg_binary(), {}, time.time() + 40
        for i in range(0, len(files), 400):
            if time.time() > deadline:
                break
            chunk = [p for _, p in files[i:i + 400]]
            try:
                r = subprocess.run(["rg", "-i", "-o", "-n", "--no-messages", "--with-filename", "--max-count", "2", "-e", pattern, "--"] + chunk,
                                   executable=binary, capture_output=True, text=True, timeout=30, errors="replace")
            except subprocess.TimeoutExpired:
                continue
            for line in r.stdout.splitlines():
                m = re.match(r"(.+?):(\d+):(.*)", line)
                if m and m.group(1) in mtimes:
                    hits.setdefault(m.group(1), []).append(" ".join(m.group(3).split())[:240])
        if not hits:
            return f"No match for that phrase in {len(files)} files."
        out = []
        for p in sorted(hits, key=lambda p: -mtimes[p])[:max(1, min(int(max_files), 40))]:
            out.append(f"{short(p)}  ({time.strftime('%Y-%m-%d', time.localtime(mtimes[p]))})")
            out += ["    " + REDACT.sub("[redacted]", s) for s in hits[p][:2]]
        return f"{len(hits)} files match, showing the newest.\n" + "\n".join(out)

    @tool("read_local", "Read a local file", READ)
    def read_local(path: str, start_line: int = 1, max_chars: int = 8000, from_end: bool = True) -> str:
        """Read one file found with list_local or search_local. A session transcript comes back as the conversation, user and Claude turns only; from_end true returns its latest part. Other files are returned as text from start_line. max_chars is between 500 and 40000."""
        name, real = resolve(path)
        if not os.path.isfile(real):
            raise ValueError("That is a folder. Use list_local with path.")
        n = max(500, min(int(max_chars), 40000))
        if real.endswith(".jsonl") and name == "transcripts":
            all_turns = turns(real)
            text = "\n\n".join(all_turns)
            part = text[-n:] if from_end else text[:n]
            note = f"[{len(all_turns)} turns, showing the {'last' if from_end else 'first'} {len(part)} of {len(text)} characters]\n"
            return note + REDACT.sub("[redacted]", part)
        if os.path.getsize(real) > 8_000_000:
            raise ValueError("That file is larger than 8 MB.")
        raw = open(real, "rb").read()
        if b"\0" in raw[:4096]:
            raise ValueError("That is not a text file.")
        lines = raw.decode("utf-8", "replace").splitlines()
        start = max(1, int(start_line))
        text = "\n".join(lines[start - 1:])
        return f"[{len(lines)} lines, from line {start}{', cut' if len(text) > n else ''}]\n" + REDACT.sub("[redacted]", text[:n])

    # ------------------------------------------------------------------ app

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True, host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[host, f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=["https://claude.ai", "https://claude.com", public]))
    GUARDED = ("/mcp", "/register", "/token", "/revoke")

    async def guarded(scope, receive, send):
        """Machine endpoints answer only to the documented Claude address range. Browser pages stay open."""
        if scope["type"] == "http" and nets and scope["path"].startswith(GUARDED):
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            ip = headers.get("cf-connecting-ip") or (headers.get("x-forwarded-for", "").split(",")[0].strip())
            if ip:  # a request with neither header came from this machine itself
                try:
                    ok = any(ipaddress.ip_address(ip) in net for net in nets)
                except ValueError:
                    ok = False
                if not ok:
                    audit("ip_refused", ip=ip, path=scope["path"])
                    await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": b'{"error":"forbidden"}'})
                    return
        await app(scope, receive, send)

    audit("start", url=public, port=port)
    uvicorn.run(guarded, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False, access_log=False)


if __name__ == "__main__":
    cli(sys.argv[1:])

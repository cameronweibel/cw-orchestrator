#!/usr/bin/env python3
"""Build a per-workspace CODEX_HOME for codex-ws.

Mirrors what claude-ws hands to Claude Code, but in the shape Codex wants:

  claude-ws                                 codex-ws
  --------------------------------------    --------------------------------------
  --mcp-config <ws>.mcp.json                [mcp_servers.*] in <home>/config.toml
  --strict-mcp-config                       a private CODEX_HOME per workspace
  --append-system-prompt "<ws>/CLAUDE.md"   <home>/AGENTS.md
  (global ~/.claude/CLAUDE.md, automatic)   prepended into the same AGENTS.md

Everything under the generated home is disposable: it is rebuilt on every
launch, so editing the profile json or the workspace CLAUDE.md is enough.
Auth, plugins, skills and caches are shared with the real ~/.codex through
links so a workspace session never needs its own login.

Prints one `-c key=value` line per config override the launch needs, which
codex-ws passes through to codex.

Usage: codex-ws-profile.py <workspace> <ws_dir> <profile.json> <base_home> <target_home> <global_md>
"""
import json
import os
import shutil
import socket
import sys
import tomllib

# Shared with the real ~/.codex: logging in once is enough, plugins and model
# caches stay in one place. Sessions, history and logs stay per workspace so
# `codex resume` inside a workspace only offers that workspace's threads.
SHARED = [
    "auth.json",
    "installation_id",
    "models_cache.json",
    "plugins",
    "skills",
    "cache",
    "vendor_imports",
    "node_repl",
]

BARE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def key(k):
    return k if k and set(k) <= BARE else json.dumps(k)


def value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(value(x) for x in v) + "]"
    raise TypeError("cannot serialise %r" % (v,))


def dump(table, path, out):
    """Write scalars for this table, then recurse into sub tables."""
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in table.items() if isinstance(v, dict)}
    if path:
        out.append("[%s]" % ".".join(key(p) for p in path))
    for k, v in scalars.items():
        out.append("%s = %s" % (key(k), value(v)))
    if scalars or path:
        out.append("")
    for k, v in tables.items():
        dump(v, path + [k], out)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def port_overrides(name, env):
    """Per launch `-c` overrides that move a server off its fixed port.

    The Google Workspace MCP binds WORKSPACE_MCP_PORT on startup and dies if
    it is taken, which makes Codex drop the whole namespace with a handshake
    error. The port is fixed per workspace in the profile json, so a Claude
    session from claude-ws holds it for as long as it runs, and two Codex
    sessions in the same workspace would fight over it as well. Handing each
    launch its own free port on the command line removes both collisions and
    leaves the file itself untouched, so nothing races on the config.

    The fixed port only matters for the OAuth redirect uri during a first
    authorization. Run that from claude-ws, or set the port by hand.
    """
    out = []
    for key, value in env.items():
        if key.endswith("_PORT") and str(value).isdigit():
            out.append('mcp_servers.%s.env.%s="%s"' % (name, key, free_port()))
    return out


def to_codex_server(name, spec):
    """One entry of an .mcp.json mcpServers block, in Codex config shape."""
    out = {}
    kind = spec.get("type", "stdio")
    if kind in ("http", "sse", "streamable-http"):
        # Codex talks to remote servers by url. Headers carrying secrets are
        # not portable, so they are reported rather than silently dropped.
        out["url"] = spec["url"]
        if spec.get("headers"):
            print("codex-ws: %s: dropped http headers, Codex has no equivalent"
                  % name, file=sys.stderr)
    else:
        out["command"] = spec["command"]
        if spec.get("args"):
            out["args"] = list(spec["args"])
        if spec.get("env"):
            out["env"] = dict(spec["env"])
        # uvx resolves the package on first run, which can outrun the default.
        out["startup_timeout_sec"] = 120
    return out


def link_shared(base_home, target_home):
    for entry in SHARED:
        src = os.path.join(base_home, entry)
        dst = os.path.join(target_home, entry)
        if not os.path.exists(src):
            continue
        if os.path.islink(dst) and os.path.realpath(dst) == os.path.realpath(src):
            continue
        # Codex rewrites auth.json on token refresh, and an atomic rename
        # replaces the link with a real file. When that happened, push the
        # newer credentials back to ~/.codex before relinking, so refreshing
        # in one workspace keeps every other workspace logged in.
        if os.path.exists(dst) and not os.path.islink(dst):
            try:
                if os.path.getmtime(dst) > os.path.getmtime(src):
                    shutil.copy2(dst, src)
            except OSError:
                pass
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        elif os.path.islink(dst):
            os.remove(dst)
        os.symlink(src, dst)


def main():
    ws, ws_dir, profile_path, base_home, target_home, global_md = sys.argv[1:7]

    os.makedirs(target_home, exist_ok=True)
    link_shared(base_home, target_home)

    base_config = {}
    base_path = os.path.join(base_home, "config.toml")
    if os.path.exists(base_path):
        with open(base_path, "rb") as fh:
            base_config = tomllib.load(fh)

    with open(profile_path) as fh:
        profile = json.load(fh)

    overrides = []
    servers = dict(base_config.get("mcp_servers") or {})
    # Servers inherited from the base config point at ~/.codex in their env
    # (CODEX_HOME, trusted path lists). Re-aim them at this workspace home,
    # or they would read and trust the wrong one.
    for spec in servers.values():
        env = spec.get("env")
        if not isinstance(env, dict):
            continue
        for k, v in list(env.items()):
            if not isinstance(v, str):
                continue
            if k == "CODEX_HOME":
                env[k] = target_home
            elif base_home in v.split(":") and target_home not in v.split(":"):
                env[k] = v.replace(base_home, target_home, 1) + ":" + base_home
    for name, spec in (profile.get("mcpServers") or {}).items():
        servers[name] = to_codex_server(name, spec)
        overrides += port_overrides(name, servers[name].get("env") or {})
    base_config["mcp_servers"] = servers

    # Codex ships ChatGPT connectors of its own under codex_apps, including a
    # Google Calendar and Gmail that answer for whichever account is linked to
    # the desktop app. That is the one account this whole setup exists to keep
    # separate, and the model does reach for them when both are present. Turn
    # the connectors off so the workspace MCP servers are the only route out.
    features = dict(base_config.get("features") or {})
    features["apps"] = False
    base_config["features"] = features

    # Codex refuses to run in an untrusted directory. The workspace folder is
    # the one it always starts in, so mark it trusted up front.
    projects = dict(base_config.get("projects") or {})
    projects[ws_dir] = {"trust_level": "trusted"}
    base_config["projects"] = projects

    lines = ["# Generated by codex-ws for the '%s' workspace. Do not edit." % ws,
             "# Source: %s" % profile_path, ""]
    dump(base_config, [], lines)
    with open(os.path.join(target_home, "config.toml"), "w") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")

    # Claude Code loads the global CLAUDE.md by itself and claude-ws appends
    # the workspace one. Codex reads a single AGENTS.md, so both go in it.
    parts = []
    for src in (global_md, os.path.join(ws_dir, "CLAUDE.md")):
        if os.path.isfile(src) and os.path.getsize(src):
            with open(src) as fh:
                parts.append(fh.read().rstrip())
    header = ("<!-- Generated by codex-ws for the '%s' workspace. Do not edit. -->"
              % ws)
    with open(os.path.join(target_home, "AGENTS.md"), "w") as fh:
        fh.write(header + "\n\n" + "\n\n".join(parts) + "\n")

    # stdout is the launcher's contract: one `-c key=value` override per line.
    for override in overrides:
        print(override)


if __name__ == "__main__":
    main()

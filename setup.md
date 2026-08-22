# cw orchestrator setup skill

You are Claude Code, running inside a clone of the cw-orchestrator repo. The user wants you to install and configure cw and claude-ws on this machine. Follow these steps in order, talk to the user where marked, and verify each step before moving on. README.md explains what the pieces are; this file is your runbook.

## Step 1: dependencies

Check for `tmux`, `fzf`, `jq`, `curl`, and `claude` with `command -v`. Install anything missing with Homebrew (`brew install tmux fzf jq`). If `claude` is missing, stop and point the user at https://claude.com/claude-code, since the whole system runs on the Claude Code CLI and its login. If this is not macOS on Apple Silicon, warn the user that the scripts reference `/opt/homebrew/bin` and offer to patch those paths after install.

## Step 2: install the files

Run `./install.sh` from the repo root and show the user its output. It copies `bin/cw` and `bin/claude-ws` to `~/.claude/bin`, seeds `~/.claude/orchestrator/` (empty.mcp.json, routing.md, master.md) and `~/.claude/workspaces.json`, and never overwrites existing config. If `~/.claude/bin` is not on the user's PATH, add the export line to their shell profile after asking which file they use (.zshrc usually).

## Step 3: interview the user about their workspaces

Ask the user what workspaces they want. A workspace is an area of life or business that should have its own context and, usually, its own accounts: for example work, personal, or one per company. For each workspace collect: a short lowercase name, a one sentence description, the topics and names that identify it, and which MCP servers it needs (most importantly which Google or email account, if they use an email MCP).

## Step 4: create the profiles

For each workspace write `~/.claude/profiles/<name>.mcp.json` containing exactly the MCP servers that workspace may use, modeled on `templates/example.mcp.json`. The file name is the workspace name. If the user has no MCP servers yet, an empty `{"mcpServers": {}}` profile is valid; the workspace still gets its own CLAUDE.md layer and routing. Remove the seeded `example.mcp.json` once real profiles exist. Remind the user that claude-ws passes `--strict-mcp-config`, so each session sees only its own profile's servers.

## Step 5: teach the router

Rewrite `~/.claude/workspaces.json` with one entry per workspace (name, display_name, description) and rewrite `~/.claude/orchestrator/routing.md` with one `## <name>` section per workspace, filled with the topics and names from the interview, plus a Tie breakers section. The headings must match the profile names exactly, since corrections are appended under the matching heading. Both files are plain language; write them well, they are the router's entire brain.

## Step 6: optional per workspace context

Offer to create `~/.claude/workspaces/<name>/CLAUDE.md` for each workspace with any standing instructions the user wants that workspace's sessions to follow, and `workspace.env` for environment variables. Both are optional and can be added later.

## Step 7: smoke test

Run these and show the user the results:

1. `cw route "<a task the user would type for workspace A>"` and the same for workspace B. Confirm the routing decisions look right. Explicit names skip the agent; everything else takes a few seconds because a model call decides.
2. `cw -d <workspace> "Reply with exactly the word READY and then wait. Do not use tools."` then `cw ls` (expect the session listed, state busy then idle), `cw peek <id>` (expect READY), `cw send <id> "Reply with exactly PONG"`, wait a few seconds, `cw peek <id>` (expect PONG), then `cw kill <id>`.
3. `cw home -d` to build the control room layout without attaching, then check `tmux -L cwhome list-panes -t home` shows a 44 column list pane and a chat pane. Tell the user to run `cw home` themselves in a real terminal to attach.

If the user's terminal multiplexes through tmux already, note that cw pins all its tmux calls to the default server, so nesting is handled.

## Step 8: hand over

Summarize for the user: the commands from the README's command reference, how the routing correction prompt teaches routing.md, that `cw master` gives them an orchestrator chat, and that `cw home` is the always on control room. Point them at docs/cw-orchestrator-design.html for the full design.

## Gotchas you must respect while working

- Claude CLI flags like `--mcp-config` and `--allowedTools` are variadic and swallow a following positional prompt. Use `--mcp-config=path` and put positional prompts before such flags. The shipped scripts already do this; keep it that way if you edit them.
- tmux windows spawn from a non login shell. Any command string you hand to tmux must export PATH first if it calls cw or claude.
- Do not "fix" the `tmux()` wrapper in cw that pins `-L default`. Inside the cw home outer session, $TMUX points at the wrong socket without it.
- The cw home list pane width is pinned by client-attached and client-resized hooks. Without them, attaching from a smaller terminal crushes the pane to one column.
- The list refresher runs with errexit disabled and every command guarded. A single unguarded failing command kills it silently and freezes the list.

#!/usr/bin/env bash
# Installs cw and claude-ws into ~/.claude and seeds the config files.
# Safe to rerun: existing config files are never overwritten.
set -euo pipefail

repo="$(cd "$(dirname "$0")" && pwd)"
bin_dir="$HOME/.claude/bin"
orc_dir="$HOME/.claude/orchestrator"
profiles_dir="$HOME/.claude/profiles"
workspaces_dir="$HOME/.claude/workspaces"

echo "== cw orchestrator installer =="

missing=""
for dep in tmux fzf jq curl claude; do
  command -v "$dep" >/dev/null 2>&1 || missing="$missing $dep"
done
if [[ -n "$missing" ]]; then
  echo "Missing dependencies:$missing"
  echo "On macOS: brew install tmux fzf jq"
  echo "Claude Code: https://claude.com/claude-code"
  exit 1
fi

mkdir -p "$bin_dir" "$orc_dir" "$profiles_dir" "$workspaces_dir"

# The scripts are always updated to the repo version.
cp "$repo/bin/cw" "$bin_dir/cw"
cp "$repo/bin/claude-ws" "$bin_dir/claude-ws"
chmod +x "$bin_dir/cw" "$bin_dir/claude-ws"
echo "Installed $bin_dir/cw and $bin_dir/claude-ws"

# Config files are only seeded when absent.
seed() {
  local src="$1" dst="$2"
  if [[ -e "$dst" ]]; then
    echo "Kept existing $dst"
  else
    cp "$src" "$dst"
    echo "Seeded $dst"
  fi
}
seed "$repo/templates/empty.mcp.json" "$orc_dir/empty.mcp.json"
seed "$repo/templates/routing.md"     "$orc_dir/routing.md"
seed "$repo/templates/master.md"      "$orc_dir/master.md"
seed "$repo/templates/workspaces.json" "$HOME/.claude/workspaces.json"

if ! ls "$profiles_dir"/*.mcp.json >/dev/null 2>&1; then
  cp "$repo/templates/example.mcp.json" "$profiles_dir/example.mcp.json"
  echo "Seeded $profiles_dir/example.mcp.json (rename it and add your MCP servers)"
fi

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo ""
     echo "Add this to your shell profile:"
     echo "  export PATH=\"\$HOME/.claude/bin:\$PATH\"" ;;
esac

echo ""
echo "Next steps (details in README.md):"
echo "  1. Create one <name>.mcp.json per workspace in $profiles_dir"
echo "  2. Describe your workspaces in ~/.claude/workspaces.json and $orc_dir/routing.md"
echo "  3. Try: cw route \"some task\"    then: cw \"some task\"    then: cw home"

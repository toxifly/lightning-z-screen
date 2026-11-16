#!/usr/bin/env bash
# Install the Claude sessions feed for the Lightning Z screen, for the current user (no sudo):
#   ~/.local/bin/claude-board, Claude Code hooks in ~/.claude/settings.json and the systemd user
#   unit claude-board.service. The screen service itself is updated with deploy/install.sh.
# Run:  bash deploy/install-claude-board.sh          Remove:  bash deploy/install-claude-board.sh --uninstall
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$HOME/.local/bin/claude-board"
UNIT="$HOME/.config/systemd/user/claude-board.service"
SETTINGS="$HOME/.claude/settings.json"

hooks() {   # $1 = add | remove
  python3 -I - "$1" "$SETTINGS" "$BIN" <<'EOF'
import json, os, sys
action, path, binary = sys.argv[1:]
cmd = f"{binary} hook"
want = {"SessionStart": "", "SessionEnd": "", "UserPromptSubmit": None, "Stop": None,
        "Notification": "", "PreToolUse": "AskUserQuestion|ExitPlanMode", "PostToolUse": ""}
try:
    with open(path) as f: s = json.load(f)
except FileNotFoundError:
    s = {}
h = s.setdefault("hooks", {})
for ev in list(h):                                    # drop ours everywhere first (idempotent)
    for grp in h[ev]:
        grp["hooks"] = [x for x in grp.get("hooks", []) if x.get("command") != cmd]
    h[ev] = [g for g in h[ev] if g.get("hooks")]
    if not h[ev]: del h[ev]
if action == "add":
    for ev, matcher in want.items():
        grp = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
        if matcher is not None: grp = {"matcher": matcher, **grp}
        h.setdefault(ev, []).append(grp)
if not h: del s["hooks"]
tmp = path + ".tmp"
with open(tmp, "w") as f: json.dump(s, f, indent=2); f.write("\n")
os.replace(tmp, path)
EOF
}

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now claude-board.service 2>/dev/null || true
  rm -f "$UNIT" "$BIN"; systemctl --user daemon-reload
  hooks remove
  echo "Removed. Session state in ~/.local/state/claude-board can be deleted."
  exit 0
fi

install -D -m 755 "$SRC/claude-board" "$BIN"
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<EOF
[Unit]
Description=Claude Code sessions feed for the Lightning Z screen

[Service]
ExecStart=$BIN run
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
cp "$SETTINGS" "$SETTINGS.bak-claude-board" 2>/dev/null || true
hooks add
systemctl --user daemon-reload
systemctl --user enable --now claude-board.service
systemctl --user restart claude-board.service
systemctl --user --no-pager --lines=3 status claude-board.service || true
echo "Hooks added to $SETTINGS (backup: $SETTINGS.bak-claude-board)."
echo "Claude sessions started before this report once they get their next message."

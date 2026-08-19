#!/usr/bin/env bash
# Mastisk installer for macOS. Idempotent — safe to re-run.
#
#   ./install.sh                install + init (no autostart)
#   ./install.sh --autostart    install + init + launchd agent
#   ./install.sh --demo         install + init --demo (loads sample wiki)
#   ./install.sh --update       git pull, rebuild, reinstall, restart
#   ./install.sh --uninstall    remove everything except iCloud vault
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ────────────────────────────────────────────────────────────────── args ──
AUTOSTART=0
DEMO=0
UNINSTALL=0
UPDATE=0
for arg in "$@"; do
  case "$arg" in
    --autostart)   AUTOSTART=1 ;;
    --demo)        DEMO=1 ;;
    --update)      UPDATE=1 ;;
    --uninstall)   UNINSTALL=1 ;;
    -h|--help)
      sed -n '1,/^set -euo/p' "$0" | sed -n '1,/^set -euo/{/^# /p}'
      exit 0
      ;;
    *) echo "unknown arg: $arg"; exit 1 ;;
  esac
done

# ─────────────────────────────────────────────────────── claude skills ──
# Skills this repo owns. They install as symlinks into ~/.claude/skills/ so
# they track the checkout instead of drifting as private copies.
SKILL_NAMES=(mastisk guru)

skill_source() {
  case "$1" in
    mastisk) printf '%s\n' "$REPO_ROOT/SKILL.md" ;;
    guru)    printf '%s\n' "$REPO_ROOT/skills/guru/SKILL.md" ;;
  esac
}

install_skills() {
  local name src dest dest_dir stamp
  for name in "${SKILL_NAMES[@]}"; do
    src="$(skill_source "$name")"
    if [ ! -f "$src" ]; then
      echo "  ! $name skill missing at $src — skipped"
      continue
    fi
    dest_dir="$HOME/.claude/skills/$name"
    dest="$dest_dir/SKILL.md"
    mkdir -p "$dest_dir"
    if [ -L "$dest" ]; then
      ln -sfn "$src" "$dest"
    elif [ -f "$dest" ]; then
      # A real file is already there. Keep it if the user changed it.
      if cmp -s "$dest" "$src"; then
        rm -f "$dest"
      else
        stamp="$(date +%Y%m%d%H%M%S)"
        mv "$dest" "$dest.local.$stamp"
        echo "  ! $name/SKILL.md had local edits — saved as SKILL.md.local.$stamp"
      fi
      ln -s "$src" "$dest"
    else
      ln -s "$src" "$dest"
    fi
    echo "✓ /$name skill → $src"
  done
}

remove_skills() {
  local name dest target
  for name in "${SKILL_NAMES[@]}"; do
    dest="$HOME/.claude/skills/$name/SKILL.md"
    [ -L "$dest" ] || continue
    target="$(readlink "$dest")"
    # Only unlink what this checkout installed.
    case "$target" in
      "$REPO_ROOT"/*)
        rm -f "$dest"
        rmdir "$HOME/.claude/skills/$name" 2>/dev/null || true
        echo "✓ unlinked /$name skill"
        ;;
    esac
  done
}

# ─────────────────────────────────────────────────────────── update ──
if [ "$UPDATE" -eq 1 ]; then
  echo "== Updating Mastisk =="
  git pull --ff-only
  echo "== Rebuilding frontend =="
  (cd frontend && npm install --silent && npm run build) >/dev/null
  echo "== Reinstalling Python package =="
  uv tool install --force --reinstall . >/dev/null
  # Refresh install_source breadcrumb
  MASTISK_CFG_DIR="$HOME/Library/Application Support/Mastisk"
  mkdir -p "$MASTISK_CFG_DIR"
  printf '%s\n' "$REPO_ROOT" > "$MASTISK_CFG_DIR/install_source"
  echo "✓ binary refreshed at $(command -v mastisk || echo '~/.local/bin/mastisk')"
  echo "== Refreshing Claude Code skills =="
  install_skills
  # Restart if launchd agent is loaded
  if launchctl list 2>/dev/null | grep -q 'com.mastisk.agents'; then
    echo "== Restarting via launchctl =="
    launchctl kickstart -k "gui/$(id -u)/com.mastisk.agents"
    echo "✓ restarted"
  else
    echo "  (no autostart agent; if Mastisk was running, restart it: pkill -f 'mastisk start' && mastisk start &)"
  fi
  mastisk --version 2>/dev/null || true
  exit 0
fi

# ─────────────────────────────────────────────────────────── uninstall ──
if [ "$UNINSTALL" -eq 1 ]; then
  echo "uninstalling mastisk..."
  mastisk disable-autostart 2>/dev/null || true
  remove_skills
  uv tool uninstall mastisk 2>/dev/null || true
  rm -rf "$HOME/Library/Application Support/Mastisk"
  echo "✓ gone."
  echo "  iCloud vault preserved at ~/Library/Mobile Documents/com~apple~CloudDocs/Mastisk"
  echo "  delete it manually if you also want to remove your markdown."
  exit 0
fi

# ─────────────────────────────────────────────────────────── prereqs ──
echo "== Mastisk install =="
echo ""

need_brew=0
missing=""

check() {
  local name="$1" hint="$2"
  if ! command -v "$name" >/dev/null 2>&1; then
    # Also check common non-PATH locations (e.g. Tailscale.app)
    if [ "$name" = "tailscale" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
      echo "✓ tailscale /Applications/Tailscale.app (not on PATH, that's fine)"
      return
    fi
    echo "✗ $name missing  — $hint"
    missing="$missing $name"
  else
    local ver
    ver="$($name --version 2>/dev/null | head -1 || echo '?')"
    echo "✓ $name  $ver"
  fi
}

check uv        "curl -LsSf https://astral.sh/uv/install.sh | sh"
check node      "brew install node"
check claude    "https://claude.com/claude-code  (log in with: claude login)"
check ollama    "brew install ollama  (optional — enables local models + cloud proxy)"
check tailscale "brew install --cask tailscale  (optional — for phone access from anywhere)"

if [ -n "$missing" ]; then
  echo ""
  echo "Install missing prereqs above, then re-run ./install.sh"
  exit 1
fi

# ─────────────────────────────────────────────────────────── frontend ──
echo ""
echo "== Building frontend =="
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install)
fi
(cd frontend && npm run build) >/dev/null
echo "✓ frontend built into src/mastisk/pwa/"

# ─────────────────────────────────────────────────────────── python pkg ──
echo ""
echo "== Installing mastisk (uv tool) =="
# --force lets re-runs pick up code changes
uv tool install --force --reinstall . >/dev/null
echo "✓ mastisk installed to ~/.local/share/uv/tools/mastisk"
echo "✓ binary at $(command -v mastisk || echo '~/.local/bin/mastisk')"

# Breadcrumb for `mastisk update` — tells the command where this repo lives
MASTISK_CFG_DIR="$HOME/Library/Application Support/Mastisk"
mkdir -p "$MASTISK_CFG_DIR"
printf '%s\n' "$REPO_ROOT" > "$MASTISK_CFG_DIR/install_source"

# ─────────────────────────────────────────────────────── claude skills ──
echo ""
echo "== Installing Claude Code skills =="
install_skills

# ─────────────────────────────────────────────────────────── embed model ──
if command -v ollama >/dev/null 2>&1; then
  if ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -q '^nomic-embed-text'; then
    echo ""
    echo "== Pulling nomic-embed-text (for Scout's interest-gating + Ask retrieval) =="
    ollama pull nomic-embed-text || echo "  (skip — you can run 'ollama pull nomic-embed-text' later)"
  fi
fi

# ─────────────────────────────────────────────────────────── init ──
echo ""
echo "== Initializing =="
if [ ! -f "$HOME/Library/Application Support/Mastisk/config.toml" ]; then
  if [ "$DEMO" -eq 1 ]; then
    mastisk init --demo
  else
    mastisk init
  fi
else
  echo "✓ config already exists at $HOME/Library/Application Support/Mastisk/config.toml"
  if [ "$DEMO" -eq 1 ]; then
    mastisk seed-demo
  fi
fi

# ─────────────────────────────────────────────────────────── autostart ──
if [ "$AUTOSTART" -eq 1 ]; then
  echo ""
  echo "== Enabling launch-at-login =="
  mastisk enable-autostart
fi

# ─────────────────────────────────────────────────────────── done ──
echo ""
echo "════════════════════════════════════════════════════════════"
echo " ✓ Mastisk installed"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "Next:"
if [ "$AUTOSTART" -eq 1 ]; then
  echo "  launchctl kickstart gui/$(id -u)/com.mastisk.agents     # start now without rebooting"
  echo "  or just reboot — it'll start automatically"
else
  echo "  mastisk start                    # run in the foreground"
  echo "  ./install.sh --autostart         # re-run to enable launch-at-login"
fi
echo ""
echo "URLs:"
mastisk url 2>/dev/null || echo "  run 'mastisk start' first, then 'mastisk url'"
echo ""
echo "Claude Code skills:"
echo "  /mastisk    recall and ingest from any session"
echo "  /guru       today's lesson, taught interactively"
echo ""
echo "Phone:"
echo "  1. Install Tailscale on your phone, sign in to the same tailnet"
echo "  2. Open the Tailnet URL above in Safari"
echo "  3. Share sheet → Add to Home Screen (it's a PWA)"
echo ""
echo "Tune the wiki:"
echo "  open ~/Library/Mobile\\ Documents/com~apple~CloudDocs/Mastisk/vault/_self/"
echo "  edit identity.md, interests.md, dislikes.md, style.md — agents read these every run"

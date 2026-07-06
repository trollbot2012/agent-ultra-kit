#!/usr/bin/env bash
# agent-ultra-kit installer (Linux/macOS)
#
#   curl -fsSL https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.sh | bash
#
# Installs into ~/.agent-ultra (own venv) + a symlink in ~/.local/bin, then
# runs the doctor. Uninstall:
#   rm -rf ~/.agent-ultra ~/.local/bin/agent-ultra
set -euo pipefail

REPO="${AGENT_ULTRA_REPO:-https://github.com/trollbot2012/agent-ultra-kit.git}"
DIR="$HOME/.agent-ultra"
VENV="$DIR/venv"

echo "== agent-ultra-kit installer =="

# 1. find python >= 3.10
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "ERROR: Python 3.10+ not found. Install python3 (and python3-venv on Debian/Ubuntu)." >&2
  exit 1
fi
echo "using $PY ($("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"

# 2. venv + install
mkdir -p "$DIR"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip --quiet
echo "installing agent-ultra-kit from $REPO ..."
"$VENV/bin/python" -m pip install --quiet "git+$REPO"

# 3. symlink on PATH
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/agent-ultra" "$HOME/.local/bin/agent-ultra"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "NOTE: add ~/.local/bin to your PATH to call 'agent-ultra' directly." ;;
esac

# 4. prove it
echo
"$VENV/bin/agent-ultra" doctor
echo
echo "Installed. Try:  agent-ultra demo"

#!/bin/bash
# SessionStart hook: install Python dependencies so the test suite (and any
# linters) run in Claude Code on the web sessions.
#
# Synchronous: the session waits for this to finish, guaranteeing deps are
# present before the agent runs anything. Switch to async mode for faster
# startup if the wait becomes a problem.
set -euo pipefail

# Only needed in the remote (web) environment; local dev manages its own venv.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

python3 -m pip install --upgrade pip >/dev/null 2>&1 || true
# Editable install puts the repo on sys.path and pulls the dev extra (pytest).
# Editable is used deliberately: it also exposes the top-level bootstrap.py
# module, which a plain install currently omits. The telegram extra is skipped
# on purpose — python-telegram-bot is unused (telegram_bot.py speaks the raw
# Bot API over requests) and it drags in a heavy, fragile dependency chain.
python3 -m pip install -e ".[dev]"

# Make bare-module imports (e.g. `from bootstrap import ...`,
# `from llm_tool import ...`) resolve when modules are run directly.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PYTHONPATH=\"${CLAUDE_PROJECT_DIR}:${CLAUDE_PROJECT_DIR}/tools\"" >> "$CLAUDE_ENV_FILE"
fi

echo "session-start hook: dependencies installed."

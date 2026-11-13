#!/bin/bash
# Set up and enter the agents environment: create/activate the venv, install
# dependencies on first run, and load API keys from the gitignored *_key.txt
# files. SOURCE this (don't execute it) so the exports + venv affect your shell:
#
#     source setup.sh
#     python3 dashboard.py        # or: python3 agents.py
#
# Keys live only in the *_key.txt files (the single source of truth the agents
# also read directly); nothing is hardcoded here. On first run the venv is
# created at ~/.venvs/agents and requirements.txt is installed; later runs just
# activate it.

# Directory of this script, so it works no matter where you source it from
# (BASH_SOURCE under bash, $0 under zsh).
_AGENTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_AGENTS_VENV="$HOME/.venvs/agents"

_agents_load_key() {   # _agents_load_key VAR_NAME key_file.txt [optional]
  local var="$1" file="$_AGENTS_DIR/$2" optional="$3" value
  if [ -f "$file" ]; then
    value="$(tr -d '\r\n' < "$file")"
    export "$var=$value"
  elif [ -z "$optional" ]; then
    echo "setup.sh: $2 not found — $var not set" >&2
  fi
}

# LUMEN_API_KEY is required (the agents call the Lumen gateway). The others are
# present in the folder but unused by the agents today, so load them quietly.
_agents_load_key LUMEN_API_KEY  lumen_key.txt
_agents_load_key GITHUB_TOKEN   github_key.txt  optional
_agents_load_key TAVILY_API_KEY tavily_key.txt  optional

unset -f _agents_load_key

# Create the venv + install deps on first run, then activate it.
if [ ! -f "$_AGENTS_VENV/bin/activate" ]; then
  echo "agents: creating venv at $_AGENTS_VENV ..."
  if python3 -m venv "$_AGENTS_VENV"; then
    source "$_AGENTS_VENV/bin/activate"
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet -r "$_AGENTS_DIR/requirements.txt" \
      && echo "agents: dependencies installed." \
      || echo "setup.sh: pip install failed — check requirements.txt" >&2
  else
    echo "setup.sh: could not create venv at $_AGENTS_VENV" >&2
  fi
else
  source "$_AGENTS_VENV/bin/activate"
fi

if [ -n "$VIRTUAL_ENV" ]; then
  echo "agents: keys loaded from *_key.txt, venv active ($(python --version 2>&1))"
else
  echo "agents: keys loaded from *_key.txt (venv not active)"
fi

# Dashboard bind address (override before sourcing to change). 127.0.0.1 = local only.
export AGENTS_HOST="${AGENTS_HOST:-127.0.0.1}"
export AGENTS_PORT="${AGENTS_PORT:-5080}"

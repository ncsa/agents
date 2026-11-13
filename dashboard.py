#!/usr/bin/env python3
"""Web dashboard for the folder-watching agents.

Shows every agent in the registry, whether it is running, its recent activity, and
lets you turn each on or off. Runs the fleet in-process via agents.Supervisor.

    python3 dashboard.py                # start the dashboard (auto-starts enabled agents)
    python3 dashboard.py --no-autostart # start the dashboard with all agents stopped
    python3 dashboard.py --dry-run      # run every agent in dry-run (no Lumen calls)

Binds to 127.0.0.1 by default (local only). Set AGENTS_HOST / AGENTS_PORT to change.
"""
import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import csv
import hmac

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash
import agents as A

HOST = os.environ.get("AGENTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENTS_PORT", "5080"))
WEB = os.path.join(HERE, "web")
USERS_FILE = os.environ.get("AGENTS_USERS", os.path.join(HERE, "users.csv"))

app = Flask(__name__)
SUP = None                                  # the Supervisor, created in main()


# --- HTTP Basic Auth against users.csv (same scheme as docent/pisuite) ----------
def load_users(path):
    """username -> password (or werkzeug hash) from a headerless CSV."""
    users = {}
    if os.path.isfile(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[0].strip():
                    users[row[0].strip()] = row[1].strip()
    return users


USERS = load_users(USERS_FILE)
if not USERS:
    print(f"WARNING: no users loaded from {USERS_FILE}; all logins will fail. "
          f"Add 'username,password' lines (passwords may be werkzeug hashes).")


def _password_ok(username, password):
    stored = USERS.get(username)
    if stored is None or password is None:
        return False
    if stored.startswith(("pbkdf2:", "scrypt:", "argon2")):
        return check_password_hash(stored, password)
    return hmac.compare_digest(stored, password)


@app.before_request
def _require_auth():
    auth = request.authorization
    if not auth or not _password_ok(auth.username, auth.password):
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="Agents Dashboard"'})
    return None


@app.route("/")
def index():
    return send_from_directory(WEB, "dashboard.html")


@app.route("/api/agents")
def api_agents():
    return jsonify({"agents": SUP.status()})


@app.route("/api/agents/<name>/start", methods=["POST"])
def api_start(name):
    if name not in SUP.agents:
        return jsonify({"error": "unknown agent"}), 404
    return jsonify({"ok": SUP.start(name), "running": SUP.agents[name].get("running")})


@app.route("/api/agents/<name>/stop", methods=["POST"])
def api_stop(name):
    if name not in SUP.agents:
        return jsonify({"error": "unknown agent"}), 404
    SUP.stop(name)
    return jsonify({"ok": True, "running": SUP.agents[name].get("running")})


@app.route("/api/agents/start-all", methods=["POST"])
def api_start_all():
    for n in SUP.agents:
        SUP.start(n)
    return jsonify({"ok": True})


@app.route("/api/agents/stop-all", methods=["POST"])
def api_stop_all():
    SUP.stop_all()
    return jsonify({"ok": True})


def main():
    global SUP
    ap = argparse.ArgumentParser(description="Agents web dashboard.")
    ap.add_argument("--no-autostart", action="store_true",
                    help="start with all agents stopped")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every agent in dry-run (no Lumen calls)")
    args = ap.parse_args()

    cli = {"dry_run": True} if args.dry_run else {}
    SUP = A.Supervisor(os.path.join(HERE, "agents.yaml"), cli_overrides=cli)
    if not args.no_autostart:
        SUP.start_enabled()
    print(f"Agents dashboard: http://{HOST}:{PORT}  ({len(SUP.agents)} agent(s))")
    try:
        app.run(host=HOST, port=PORT, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        SUP.stop_all()


if __name__ == "__main__":
    main()

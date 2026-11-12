#!/usr/bin/env python3
"""NCSA Lumen AI gateway — endpoint and API key resolution.

Standard library only, so the watcher stays lightweight. The key comes from
$LUMEN_API_KEY, or a lumen_key.txt file (gitignored) beside this module.
"""
import os
import sys

BASE_URL = "https://lumen.ncsa.illinois.edu/v1"

_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lumen_key.txt")


def api_key():
    """Return the Lumen API key, or exit with a message if none is configured."""
    key = os.environ.get("LUMEN_API_KEY")
    if not key and os.path.exists(_KEY_FILE):
        with open(_KEY_FILE) as f:
            key = f.read().strip()
    if not key:
        sys.exit("Set LUMEN_API_KEY or create lumen_key.txt with your Lumen key.")
    return key

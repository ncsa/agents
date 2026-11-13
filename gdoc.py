#!/usr/bin/env python3
"""Google Drive / Docs access for the agents.

Reads Google Docs and posts comments **as the signed-in user**, so comments are
authored by you (not a robot). Auth is OAuth for a real user account, cached in
token.json next to this file.

Setup (once):
  1. In Google Cloud (a project on an account whose org allows API access — e.g. a
     personal account), enable the **Google Drive API**.
  2. Configure the OAuth consent screen. For a personal (consumer) account it is
     "External"; set the publishing status to **Production** so the refresh token
     does not expire every 7 days. Add the Drive scope.
  3. Create an OAuth client ID of type **Desktop app**, download its JSON, and save
     it here as  oauth_client.json  (gitignored).
  4. Run:  python3 gdoc.py --login   (a browser opens; if you see "Google hasn't
     verified this app", it's your own app — Advanced → continue.)

Then:
  python3 gdoc.py --whoami
  python3 gdoc.py --list  <FOLDER_ID>        # Google Docs directly in a folder
  python3 gdoc.py --read  <FILE_ID>          # plain-text body of a Doc
  python3 gdoc.py --comments <FILE_ID>       # existing comments
  python3 gdoc.py --comment <FILE_ID> "text" # add a comment (as you)
"""
import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_FILE = os.path.join(HERE, "oauth_client.json")
TOKEN_FILE = os.path.join(HERE, "token.json")

# Full Drive scope: needed to read, and to create comments on, Docs that were
# shared with you (narrower scopes like drive.file only see app-created files).
SCOPES = ["https://www.googleapis.com/auth/drive"]
GDOC_MIME = "application/vnd.google-apps.document"


# --- auth -----------------------------------------------------------------

def _save(creds):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def credentials(interactive=True):
    """Return valid user credentials, refreshing or running the consent flow as
    needed. Pass interactive=False in the unattended agent (never opens a browser)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except Exception:                                  # noqa: BLE001
            creds = None
    if not interactive:
        raise RuntimeError("No valid Google credentials — run: python3 gdoc.py --login")
    if not os.path.exists(CLIENT_FILE):
        sys.exit(f"Missing {CLIENT_FILE} — download an OAuth 'Desktop app' client "
                 "JSON from Google Cloud and save it there.")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    _save(creds)
    return creds


_svc = {}


def drive():
    """A cached Drive v3 client. Uses existing credentials without prompting."""
    if "drive" not in _svc:
        from googleapiclient.discovery import build
        _svc["drive"] = build("drive", "v3", credentials=credentials(interactive=False),
                              cache_discovery=False)
    return _svc["drive"]


# --- read -----------------------------------------------------------------

def whoami():
    return drive().about().get(fields="user").execute().get("user", {})


def list_docs(folder_id):
    """Google Docs directly inside a folder: [{id, name, modifiedTime}], newest first."""
    q = (f"'{folder_id}' in parents and mimeType='{GDOC_MIME}' and trashed=false")
    out, token = [], None
    svc = drive()
    while True:
        resp = svc.files().list(
            q=q, pageToken=token, pageSize=100, orderBy="modifiedTime desc",
            fields="nextPageToken, files(id,name,modifiedTime)",
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        out += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def extract_id(ref):
    """Accept a Drive/Docs URL or a bare ID and return the ID.
    Handles .../document/d/<id>/..., .../folders/<id>, and ?id=<id> forms."""
    import re
    ref = (ref or "").strip()
    m = (re.search(r"/d/([a-zA-Z0-9_-]+)", ref)
         or re.search(r"/folders/([a-zA-Z0-9_-]+)", ref)
         or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", ref))
    return m.group(1) if m else ref


def doc_meta(file_id):
    """{id, name, modifiedTime} for one file."""
    return drive().files().get(
        fileId=file_id, fields="id,name,modifiedTime", supportsAllDrives=True).execute()


def read_text(file_id):
    """Plain-text export of a Google Doc."""
    data = drive().files().export(fileId=file_id, mimeType="text/plain").execute()
    return data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)


def list_comments(file_id, include_resolved=True):
    """Existing comments on a file: [{content, author, resolved}]."""
    out, token = [], None
    svc = drive()
    while True:
        resp = svc.comments().list(
            fileId=file_id, pageToken=token, pageSize=100,
            fields="nextPageToken, comments(content,resolved,author/displayName)").execute()
        out += resp.get("comments", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    if not include_resolved:
        out = [c for c in out if not c.get("resolved")]
    return out


def list_threads(file_id, include_resolved=False):
    """Comment threads with their replies and author identity (author.me marks the
    signed-in user). [{id, content, resolved, author, replies:[{id,content,author,...}]}]."""
    out, token = [], None
    svc = drive()
    fields = ("nextPageToken, comments(id,content,resolved,author/displayName,author/me,"
              "replies(id,content,createdTime,author/displayName,author/me))")
    while True:
        resp = svc.comments().list(fileId=file_id, pageToken=token, pageSize=100,
                                   fields=fields).execute()
        out += resp.get("comments", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    if not include_resolved:
        out = [c for c in out if not c.get("resolved")]
    return out


# --- write ----------------------------------------------------------------

def add_comment(file_id, text):
    """Add a document-level comment (authored by the signed-in user). Returns its id.
    Note: the Drive API does not expose stable text-anchoring for Google Docs, so
    comments are not pinned to a specific line — quote the relevant text in `text`."""
    return drive().comments().create(
        fileId=file_id, body={"content": text}, fields="id").execute()


def reply_to_comment(file_id, comment_id, text):
    """Post a reply in an existing comment thread (authored by the signed-in user)."""
    return drive().replies().create(
        fileId=file_id, commentId=comment_id, body={"content": text}, fields="id").execute()


def resolve_comment(file_id, comment_id):
    """Mark a comment thread resolved (via a resolve reply, as the signed-in user)."""
    return drive().replies().create(
        fileId=file_id, commentId=comment_id, body={"action": "resolve"},
        fields="id,action").execute()


def resolve_mine(file_id):
    """Resolve every unresolved comment authored by the signed-in user. Returns count."""
    n = 0
    for c in list_threads(file_id):
        if (c.get("author") or {}).get("me"):
            try:
                resolve_comment(file_id, c["id"])
                n += 1
            except Exception:                              # noqa: BLE001
                pass
    return n


# --- cli ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Google Drive/Docs helper for the agents.")
    ap.add_argument("--login", action="store_true", help="run the OAuth consent flow and cache the token")
    ap.add_argument("--whoami", action="store_true", help="print the signed-in account")
    ap.add_argument("--list", metavar="FOLDER_ID", help="list Google Docs in a folder")
    ap.add_argument("--read", metavar="FILE_ID", help="print a Doc's text")
    ap.add_argument("--comments", metavar="FILE_ID", help="list a Doc's comments")
    ap.add_argument("--comment", nargs=2, metavar=("FILE_ID", "TEXT"), help="add a comment")
    ap.add_argument("--resolve-mine", metavar="FILE_ID",
                    help="resolve every unresolved comment you authored on this file")
    args = ap.parse_args()

    if args.login:
        credentials(interactive=True)
        u = whoami()
        print(f"Signed in as {u.get('displayName')} <{u.get('emailAddress')}>")
    elif args.whoami:
        u = whoami()
        print(f"{u.get('displayName')} <{u.get('emailAddress')}>")
    elif args.list:
        for d in list_docs(args.list):
            print(f"{d['id']}  {d['modifiedTime']}  {d['name']}")
    elif args.read:
        print(read_text(args.read))
    elif args.comments:
        for c in list_comments(args.comments):
            who = (c.get("author") or {}).get("displayName", "?")
            print(f"- [{who}] {c.get('content')}")
    elif args.comment:
        cid = add_comment(args.comment[0], args.comment[1])
        print(f"added comment {cid.get('id')}")
    elif args.resolve_mine:
        print(f"resolved {resolve_mine(args.resolve_mine)} of your comment(s)")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

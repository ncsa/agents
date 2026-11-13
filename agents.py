#!/usr/bin/env python3
"""A lightweight folder-watching agent runner.

Each agent is a self-contained folder under agents/ with its own config.yaml and
Prompt.md (e.g. agent/Yanello/). The top-level agents.yaml says which agents to
activate. When run, this starts every activated agent — each watches its own folder
and, when changed files settle, sends their contents to Lumen using its Prompt.md,
then saves the response in that agent's folder.

    python3 agents.py                   # run every agent listed in agents.yaml
    python3 agents.py --agent Yanello   # run just one agent folder
    python3 agents.py --config PATH     # run one explicit config.yaml (ad hoc)
    python3 agents.py --once            # one check-and-act pass per agent, then exit
    python3 agents.py --dry-run         # assemble + save context, never call Lumen
    python3 agents.py --process-existing  # act on files already present, then watch

Agents run in parallel threads, so a slow model call in one agent never stalls the
others' watching. Change detection is by polling (snapshot of size+mtime), reliable
over network and Google Drive for Desktop mounts where OS file-watch events are not.
A changed file must look identical for `stable_polls` consecutive checks before it is
acted on, so a half-written or mid-sync file is not read prematurely.
"""
import os
import sys
import json
import time
import argparse
import fnmatch
import threading
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "watch_dir": "./inbox", "prompt": "Prompt.md", "model": "glm-5.3",
    "interval": 5, "stable_polls": 1, "events": ["added", "modified"],
    "recursive": True, "include": [], "exclude": [], "max_file_chars": 100000,
    "per_file": False, "process_existing": False, "output_dir": "responses",
    "dry_run": False, "comment_docx": False,
    # Source of what to watch: "local" (files in watch_dir) or "gdrive" (Google Docs
    # read + commented via gdoc.py as the signed-in user). For gdrive, give EITHER a
    # folder_id (watch every Doc in it) OR doc_ids (watch just those Docs). Each may
    # be a bare ID or a pasted Drive/Docs URL.
    "source": "local", "folder_id": "", "doc_ids": [],
    # Cap how many comments one pass may post (0 = no limit). 1 = a single note,
    # framed as the student's single next step. (gdrive mode always keeps a single
    # open comment and always replies to others' threads.)
    "max_notes": 0,
}


# --- config / registry ----------------------------------------------------

def _yaml_load(path):
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _abs(base, path):
    """Resolve a path relative to the agent's own folder."""
    return path if os.path.isabs(path) else os.path.join(base, path)


def load_registry(path):
    """Parse agents.yaml -> (entries, defaults). Each entry: {name, enabled, overrides}."""
    data = _yaml_load(path)
    defaults = data.get("defaults") or {}
    entries = []
    for item in (data.get("agents") or []):
        if isinstance(item, str):
            entries.append({"name": item, "enabled": True, "overrides": {}})
        elif isinstance(item, dict) and item.get("name"):
            entries.append({"name": item["name"], "enabled": item.get("enabled", True),
                            "overrides": {k: v for k, v in item.items()
                                          if k not in ("name", "enabled")}})
    return entries, defaults


def load_agent(folder, name, reg_defaults=None, cli_overrides=None, entry_overrides=None):
    """Build an agent from its folder: merged config, resolved paths, prompt text.
    Precedence: DEFAULTS < registry defaults < the agent's config.yaml < per-entry
    overrides < command-line overrides."""
    cfg_path = os.path.join(folder, "config.yaml")
    if not os.path.isfile(cfg_path):
        raise FileNotFoundError(f"{name}: no config.yaml in {folder}")
    cfg = {**DEFAULTS, **(reg_defaults or {}), **_yaml_load(cfg_path),
           **(entry_overrides or {}), **(cli_overrides or {})}
    prompt_path = _abs(folder, cfg["prompt"])
    if not os.path.isfile(prompt_path):
        raise FileNotFoundError(f"{name}: prompt file not found: {prompt_path}")
    with open(prompt_path, encoding="utf-8") as f:
        prompt_text = f.read().strip()
    out_dir = _abs(folder, cfg["output_dir"])
    os.makedirs(out_dir, exist_ok=True)
    if cfg.get("source") == "gdrive":
        if not cfg.get("folder_id") and not cfg.get("doc_ids"):
            raise FileNotFoundError(f"{name}: source is gdrive but no folder_id or doc_ids in config.yaml")
        if cfg.get("doc_ids"):
            watch_dir = f"{len(cfg['doc_ids'])} Google Doc(s)"  # display only
        else:
            watch_dir = f"Google Drive folder {cfg['folder_id']}"
    else:
        watch_dir = _abs(folder, cfg["watch_dir"])
        os.makedirs(watch_dir, exist_ok=True)
    return {"name": name, "folder": folder, "cfg": cfg, "prompt_text": prompt_text,
            "watch_dir": watch_dir, "out_dir": out_dir,
            "skip_abs": os.path.realpath(out_dir),
            "base": None, "prev_sig": {}, "stable": {}, "_gdocs": {},
            # live status (read by the dashboard)
            "running": False, "acting": False, "_act_depth": 0,
            "last_poll": None, "last_action": None,
            "actions": 0, "error": None, "recent": []}


# --- snapshot / diff ------------------------------------------------------

def _included(name, include, exclude):
    if name.startswith("."):
        return False
    if any(fnmatch.fnmatch(name, pat) for pat in exclude):
        return False
    if include:
        return os.path.splitext(name)[1].lower() in {e.lower() for e in include}
    return True


def snapshot(folder, include, exclude, recursive, skip_abs):
    """{relpath: (size, mtime_ns)} for matching files under folder."""
    snap = {}
    folder = os.path.realpath(folder)
    for root, dirs, files in os.walk(folder):
        rr = os.path.realpath(root)
        if rr == skip_abs or rr.startswith(skip_abs + os.sep):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if not recursive:
            dirs[:] = []
        for name in files:
            if not _included(name, include, exclude):
                continue
            try:
                st = os.stat(os.path.join(root, name))     # follows symlinks (Drive)
            except OSError:
                continue
            snap[os.path.relpath(os.path.join(root, name), folder)] = (st.st_size, st.st_mtime_ns)
    return snap


def diff(old, new):
    o, n = set(old), set(new)
    return {"added": sorted(n - o), "removed": sorted(o - n),
            "modified": sorted(p for p in (o & n) if old[p] != new[p])}


# --- reading file contents ------------------------------------------------

def extract_text(path, max_chars):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            txt = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        elif ext == ".docx":
            import docx
            txt = "\n".join(p.text for p in docx.Document(path).paragraphs)
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".zip", ".xlsx", ".pptx"):
            return None                                    # binary: list by name only
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                txt = f.read()
    except Exception as e:                                 # noqa: BLE001
        return f"[could not read {os.path.basename(path)}: {e}]"
    txt = txt.strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars] + f"\n…[truncated at {max_chars} chars]"
    return txt


# --- building the request + calling Lumen ---------------------------------

def format_changes(d, events):
    lines = []
    for label in ("added", "modified", "removed"):
        if label not in events:
            continue
        lines.append(label.capitalize() + ":")
        lines += ["  - " + p for p in d.get(label, [])] or ["  (none)"]
    return "\n".join(lines)


def format_files(folder, rels, max_chars):
    blocks = []
    for rel in rels:
        text = extract_text(os.path.join(folder, rel), max_chars)
        blocks.append(f"### {rel}\n" + ("[binary file — not read]" if text is None else text))
    return "\n\n".join(blocks) if blocks else "(no readable files)"


def build_messages(prompt_text, changes_text, files_text):
    return [{"role": "system", "content": prompt_text},
            {"role": "user", "content": f"CHANGES:\n{changes_text}\n\nFILES:\n{files_text}"}]


_client = None
_client_lock = threading.Lock()
def lumen_client():
    global _client
    with _client_lock:
        if _client is None:
            import lumen
            from openai import OpenAI
            _client = OpenAI(base_url=lumen.BASE_URL, api_key=lumen.api_key())
    return _client


def call_lumen(model, messages, timeout=900):
    c = lumen_client().with_options(max_retries=0, timeout=timeout)
    return c.chat.completions.create(model=model, messages=messages).choices[0].message.content.strip()


# --- acting on a settled batch --------------------------------------------

def save_response(out_dir, tag, context, response):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, f"{stamp}{('-' + tag) if tag else ''}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Agent run {stamp}\n\n## Response\n\n{response}\n\n"
                f"---\n\n## Context sent\n\n{context}\n")
    return path


def _acting(fn):
    """Decorator: mark the agent as actively taking an action (for the dashboard's live
    indicator) while fn runs. Counter-based so nested action calls don't clear it early."""
    def wrap(agent, *a, **k):
        agent["_act_depth"] = agent.get("_act_depth", 0) + 1
        agent["acting"] = True
        try:
            return fn(agent, *a, **k)
        finally:
            agent["_act_depth"] = max(0, agent.get("_act_depth", 1) - 1)
            agent["acting"] = agent["_act_depth"] > 0
    return wrap


def read_docx_comments(path):
    """Text of comments already present in a .docx (so the agent doesn't repeat
    itself). Reads word/comments.xml straight from the zip — independent of the
    python-docx read API. Returns a list of comment strings."""
    import re, zipfile
    try:
        with zipfile.ZipFile(path) as z:
            if "word/comments.xml" not in z.namelist():
                return []
            xml = z.read("word/comments.xml").decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile):
        return []
    out = []
    for c in re.findall(r"<w:comment\b.*?</w:comment>", xml, re.S):
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", c, re.S)
        joined = "".join(texts).strip()
        if joined:
            out.append(joined)
    return out


def _comment_messages(agent, rel, changes_text, body, existing):
    """System = the agent's Prompt.md plus the strict JSON output contract this mode
    needs; user = the changes, the document text, and comments already left."""
    single = agent["cfg"].get("max_notes") == 1
    if single:
        howmany = ("Return EXACTLY ONE note: the single most useful next step for them "
                   "right now (if there is a safety concern, that IS the next step). "
                   'Return {"notes": []} only if nothing is needed.')
    else:
        howmany = ('Return {"notes": []} if there is nothing new worth saying.')
    contract = (
        "OUTPUT FORMAT (required): reply with ONLY a JSON object, no prose around it:\n"
        '{"notes": [{"quote": "<a short snippet, <=12 words, copied EXACTLY from the '
        'document, marking where this note belongs>", "comment": "<your guidance>"}]}\n'
        'Use "quote": "" for a general note not tied to one spot. ' + howmany + " Do NOT "
        "repeat guidance you have already left (listed below)."
    )
    already = "\n".join(f"- {c}" for c in existing) or "(none yet)"
    user = (f"CHANGES:\n{changes_text}\n\nDOCUMENT ({rel}):\n{body}\n\n"
            f"COMMENTS YOU HAVE ALREADY LEFT ON THIS DOCUMENT:\n{already}")
    return [{"role": "system", "content": agent["prompt_text"] + "\n\n" + contract},
            {"role": "user", "content": user}]


def _parse_notes(raw):
    """Pull the notes list out of the model's reply, tolerating code fences or stray
    text. Falls back to one general note if it isn't usable JSON."""
    import re
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    obj = None
    for candidate in (s, s[s.find("{"):s.rfind("}") + 1] if "{" in s and "}" in s else ""):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            break
        except ValueError:
            continue
    if not isinstance(obj, dict) or not isinstance(obj.get("notes"), list):
        return [{"quote": "", "comment": s}] if s else []
    out = []
    for n in obj["notes"]:
        if isinstance(n, dict) and str(n.get("comment", "")).strip():
            out.append({"quote": str(n.get("quote") or "").strip(),
                        "comment": str(n["comment"]).strip()})
    return out


def insert_docx_comments(path, notes, author):
    """Add real Word comments to a .docx, anchored to the paragraph that contains each
    note's quote (unanchored notes gather into one 'mentor notes' comment at the end).
    Returns how many comments were added; saves the file only if any were."""
    import docx
    doc = docx.Document(path)
    paras = doc.paragraphs
    initials = (author[:2] or "AG").upper()
    added, general = 0, []
    for n in notes:
        text = n["comment"]
        quote = n.get("quote", "")
        target = None
        if quote:
            ql = quote.lower()
            target = next((p for p in paras if p.text and ql in p.text.lower()), None)
        if target is None:
            general.append(text)
            continue
        runs = target.runs or [target.add_run("")]
        doc.add_comment(runs, text=text, author=author, initials=initials)
        added += 1
    if general:
        anchor = doc.add_paragraph().add_run("· mentor notes ·")
        doc.add_comment([anchor], text="\n\n".join(general), author=author, initials=initials)
        added += 1
    if added:
        doc.save(path)
    return added


@_acting
def _comment_on_docx(agent, rel, changes_text, log):
    """Comment-in-place on one .docx. Returns True if the file was written."""
    cfg, folder = agent["cfg"], agent["watch_dir"]
    path = os.path.join(folder, rel)
    body = extract_text(path, cfg["max_file_chars"]) or ""
    existing = read_docx_comments(path)
    msgs = _comment_messages(agent, rel, changes_text, body, existing)
    if cfg["dry_run"]:
        tag = os.path.splitext(os.path.basename(rel))[0]
        p = save_response(agent["out_dir"], tag, msgs[-1]["content"],
                          "(dry run — would add Word comments to the .docx)")
        agent["actions"] += 1
        agent["last_action"] = {"at": time.time(), "files": [rel], "file": os.path.basename(p)}
        log(f"dry-run: would comment on {rel} -> {os.path.relpath(p, HERE)}")
        return False
    try:
        raw = call_lumen(cfg["model"], msgs)
    except Exception as e:                                 # noqa: BLE001
        log(f"ERROR calling Lumen: {e}")
        return False
    notes = _parse_notes(raw)
    if cfg.get("max_notes"):
        notes = notes[:cfg["max_notes"]]
    if not notes:
        log(f"{rel}: no new guidance")
        return False
    try:
        added = insert_docx_comments(path, notes, author=agent["name"])
    except Exception as e:                                 # noqa: BLE001
        log(f"ERROR writing comments to {rel}: {e}")
        return False
    if not added:
        log(f"{rel}: no comments added")
        return False
    agent["actions"] += 1
    agent["last_action"] = {"at": time.time(), "files": [rel], "file": rel}
    log(f"commented on {rel}: {added} note(s) written into the document")
    return True


def _thread_transcript(comment):
    lines = []
    for i, m in enumerate([comment] + (comment.get("replies") or [])):
        who = "You" if (m.get("author") or {}).get("me") else \
              ((m.get("author") or {}).get("displayName") or "Student")
        lines.append(f"{who}: {m.get('content', '')}")
    return "\n".join(lines)


def _gdrive_in_open_thread(threads):
    """True if we (the signed-in user) authored or replied in any unresolved thread —
    i.e. a mentor conversation is already open on this Doc."""
    for c in threads:
        if (c.get("author") or {}).get("me"):
            return True
        if any((r.get("author") or {}).get("me") for r in (c.get("replies") or [])):
            return True
    return False


@_acting
def _gdrive_post_comment(agent, doc_id, name, existing_contents, log):
    """Post a single new top-level coaching comment (the student's next step)."""
    import gdoc
    cfg = agent["cfg"]
    try:
        body = gdoc.read_text(doc_id)
    except Exception as e:                                 # noqa: BLE001
        log(f"ERROR reading {name}: {e}")
        return
    msgs = _comment_messages(agent, name, f"The student edited: {name}", body, existing_contents)
    if cfg["dry_run"]:
        p = save_response(agent["out_dir"], name.replace("/", "_"), msgs[-1]["content"],
                          "(dry run — would post one Drive comment)")
        agent["actions"] += 1
        agent["last_action"] = {"at": time.time(), "files": [name], "file": os.path.basename(p)}
        log(f"dry-run: would comment on {name}")
        return
    try:
        raw = call_lumen(cfg["model"], msgs)
    except Exception as e:                                 # noqa: BLE001
        log(f"ERROR calling Lumen: {e}")
        return
    notes = _parse_notes(raw)[:1]                          # one open comment = one next step
    if not notes:
        log(f"{name}: no new guidance")
        return
    n = notes[0]
    content = f'Re: "{n["quote"]}" — {n["comment"]}' if n.get("quote") else n["comment"]
    try:
        gdoc.add_comment(doc_id, content)
    except Exception as e:                                 # noqa: BLE001
        log(f"ERROR commenting on {name}: {e}")
        return
    agent["actions"] += 1
    agent["last_action"] = {"at": time.time(), "files": [name], "file": name}
    log(f"posted next-step comment on {name}")


@_acting
def _gdrive_reply(agent, doc_id, name, comment, log):
    """Reply once in a thread whose last message is from someone other than us."""
    import gdoc
    cfg = agent["cfg"]
    try:
        body = gdoc.read_text(doc_id)
    except Exception:                                      # noqa: BLE001
        body = ""
    system = agent["prompt_text"] + "\n\n" + (
        "Someone (not you) has posted in this comment thread. Continue the coaching in a "
        "single short reply (a sentence or two): answer their question or point them to "
        "their next step, still without handing over the full solution. Reply with ONLY "
        "the reply text — no JSON, no preamble.")
    user = f"DOCUMENT ({name}):\n{body}\n\nCOMMENT THREAD:\n{_thread_transcript(comment)}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if cfg["dry_run"]:
        p = save_response(agent["out_dir"], name.replace("/", "_") + "-reply", user,
                          "(dry run — would reply in the thread)")
        agent["actions"] += 1
        agent["last_action"] = {"at": time.time(), "files": [name], "file": os.path.basename(p)}
        log(f"dry-run: would reply in a thread on {name}")
        return
    try:
        text = call_lumen(cfg["model"], msgs).strip()
    except Exception as e:                                 # noqa: BLE001
        log(f"ERROR calling Lumen (reply): {e}")
        return
    if not text:
        return
    try:
        gdoc.reply_to_comment(doc_id, comment["id"], text)
    except Exception as e:                                 # noqa: BLE001
        log(f"ERROR replying on {name}: {e}")
        return
    agent["actions"] += 1
    agent["last_action"] = {"at": time.time(), "files": [name], "file": name}
    log(f"replied in a thread on {name}")


def poll_gdrive(agent, log):
    """One gdrive poll: (1) reply to any unresolved thread whose last message is from
    someone other than us, and (2) post at most one unresolved coaching comment per Doc,
    only when the content has settled and no mentor conversation is already open."""
    import gdoc
    cfg = agent["cfg"]
    agent["last_poll"] = time.time()
    cur = _snap(agent)                                     # refreshes agent["_gdocs"]
    d = diff(agent["base"], cur)
    changed = d["added"] + d["modified"]
    for p in changed:
        agent["stable"][p] = agent["stable"].get(p, 0) + 1 if agent["prev_sig"].get(p) == cur.get(p) else 0
    agent["prev_sig"] = cur
    settled = {p for p in changed if agent["stable"].get(p, 0) >= cfg["stable_polls"]}

    for doc_id, meta in (agent.get("_gdocs") or {}).items():
        name = meta.get("name", doc_id)
        try:
            threads = gdoc.list_threads(doc_id)           # unresolved only
        except Exception as e:                             # noqa: BLE001
            log(f"ERROR reading threads on {name}: {e}")
            continue
        # (1) answer anyone but ourselves: a thread whose last message isn't ours.
        for c in threads:
            chain = [c] + (c.get("replies") or [])
            if (chain[-1].get("author") or {}).get("me"):
                continue
            _gdrive_reply(agent, doc_id, name, c, log)
        # (2) offer the next step only on a settled edit, and only if no mentor thread
        #     is already open (keeps it to a single unresolved comment at a time).
        if doc_id in settled:
            try:
                threads = gdoc.list_threads(doc_id)       # re-read (we may have just replied)
            except Exception:                             # noqa: BLE001
                pass
            if not _gdrive_in_open_thread(threads):
                _gdrive_post_comment(agent, doc_id, name,
                                     [c.get("content", "") for c in threads], log)

    # Only acknowledge once a change has settled; otherwise leave the baseline so the
    # stable_polls debounce can accumulate across polls (advancing every poll would
    # mean a change never settles and nothing is ever posted).
    if settled:
        agent["base"] = cur
        for p in settled:
            agent["stable"].pop(p, None)
        save_state(agent)
    elif agent["base"] is None:
        agent["base"] = cur                                # first poll: establish baseline


@_acting
def act(agent, d, log):
    """Act on a settled batch of changes. Returns the list of watched items this call
    WROTE to (so the caller can re-baseline them and avoid self-triggering)."""
    cfg, folder, out_dir = agent["cfg"], agent["watch_dir"], agent["out_dir"]
    events = cfg["events"]
    present = [p for lbl in ("added", "modified") if lbl in events for p in d.get(lbl, [])]
    if not present and not ("removed" in events and d.get("removed")):
        return []
    changes_text = format_changes(d, events)
    written = []

    # Comment-in-place mode: write guidance back into each changed .docx as Word
    # comments. Non-.docx changes still get a saved response file.
    if cfg.get("comment_docx"):
        docx_files = [p for p in present if p.lower().endswith(".docx")]
        others = [p for p in present if not p.lower().endswith(".docx")]
        for rel in docx_files:
            if _comment_on_docx(agent, rel, changes_text, log):
                written.append(rel)
        present = others                                   # fall through for the rest
        if not present:
            return written

    batches = ([[p] for p in present] if cfg["per_file"] and present else [present])
    for batch in batches:
        files_text = format_files(folder, batch, cfg["max_file_chars"])
        msgs = build_messages(agent["prompt_text"], changes_text, files_text)
        context = msgs[1]["content"]
        tag = (os.path.splitext(os.path.basename(batch[0]))[0] if cfg["per_file"] and batch else "")
        if cfg["dry_run"]:
            path = save_response(out_dir, tag, context, "(dry run — Lumen not called)")
            agent["actions"] += 1
            agent["last_action"] = {"at": time.time(), "files": batch,
                                    "file": os.path.basename(path)}
            log(f"dry-run: {', '.join(batch) or 'removals'} -> {os.path.relpath(path, HERE)}")
            continue
        try:
            response = call_lumen(cfg["model"], msgs)
        except Exception as e:                             # noqa: BLE001
            log(f"ERROR calling Lumen: {e}")
            continue
        path = save_response(out_dir, tag, context, response)
        agent["actions"] += 1
        agent["last_action"] = {"at": time.time(), "files": batch,
                                "file": os.path.basename(path)}
        log(f"acted on {', '.join(batch) or 'removals'} -> {os.path.relpath(path, HERE)}")
    return written


# --- per-agent loop -------------------------------------------------------

def gdrive_snapshot(agent):
    """{doc_id: modifiedTime} for the watched Google Docs — either the Docs listed in
    config (doc_ids) or every Doc in folder_id. Also caches id -> {name, modifiedTime}
    on the agent so act() can read and name them."""
    import gdoc
    cfg = agent["cfg"]
    if cfg.get("doc_ids"):
        docs = []
        for ref in cfg["doc_ids"]:
            try:
                docs.append(gdoc.doc_meta(gdoc.extract_id(ref)))
            except Exception:                              # noqa: BLE001  (skip unreadable)
                continue
    else:
        docs = gdoc.list_docs(gdoc.extract_id(cfg["folder_id"]))
    agent["_gdocs"] = {d["id"]: d for d in docs}
    return {d["id"]: d["modifiedTime"] for d in docs}


def _snap(agent):
    c = agent["cfg"]
    if c.get("source") == "gdrive":
        return gdrive_snapshot(agent)
    return snapshot(agent["watch_dir"], c["include"], c["exclude"],
                    c["recursive"], agent["skip_abs"])


# --- persistent baseline (so restarts don't re-run already-handled files) --

def _state_path(agent):
    return os.path.join(agent["folder"], ".agent_state.json")


def load_state(agent):
    """The signature map of files this agent has already handled, persisted across
    restarts. Returns None when there is no saved state yet (first ever run)."""
    try:
        with open(_state_path(agent), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    base = data.get("base")
    if not isinstance(base, dict):
        return None
    # Local sigs are [size, mtime_ns] lists -> restore to tuples so diff() compares
    # like-for-like. gdrive sigs are modifiedTime strings -> leave as-is.
    return {k: (tuple(v) if isinstance(v, list) else v) for k, v in base.items()}


def save_state(agent):
    """Persist the handled-file baseline atomically; failures are non-fatal."""
    path = _state_path(agent)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"base": {k: list(v) for k, v in agent["base"].items()},
                       "saved": time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def make_logger(agent):
    """Logger that prints and keeps the last ~50 lines on the agent for the dashboard."""
    def log(msg):
        line = f"[{datetime.datetime.now():%H:%M:%S}] [{agent['name']}] {msg}"
        print(line, flush=True)
        agent["recent"].append(line)
        del agent["recent"][:-50]
    return log


def poll_once(agent, log):
    """One check-and-act cycle for an agent; updates its debounce state."""
    cfg = agent["cfg"]
    if cfg.get("source") == "gdrive":
        return poll_gdrive(agent, log)
    agent["last_poll"] = time.time()
    cur = _snap(agent)
    d = diff(agent["base"], cur)
    changed = d["added"] + d["modified"]
    for p in changed:
        agent["stable"][p] = agent["stable"].get(p, 0) + 1 if agent["prev_sig"].get(p) == cur.get(p) else 0
    agent["prev_sig"] = cur
    settled = [p for p in changed if agent["stable"].get(p, 0) >= cfg["stable_polls"]]
    has_removed = bool(d["removed"]) and "removed" in cfg["events"]
    if settled or has_removed:
        d_settled = {"added": [p for p in d["added"] if p in settled],
                     "modified": [p for p in d["modified"] if p in settled],
                     "removed": d["removed"]}
        written = act(agent, d_settled, log)
        agent["base"] = cur                                # acknowledge handled changes
        # If the agent wrote back into watched files (e.g. docx comments), re-snapshot
        # those so its OWN edit isn't seen as a new change and re-triggered in a loop.
        if written:
            post = _snap(agent)
            for rel in written:
                if rel in post:
                    agent["base"][rel] = post[rel]
                    agent["prev_sig"][rel] = post[rel]
        agent["stable"].clear()
        save_state(agent)                                  # survive restarts
    elif changed:
        log(f"change detected, waiting to settle: {', '.join(changed)}")


def agent_loop(agent, once=False, stop_event=None):
    cfg = agent["cfg"]
    log = make_logger(agent)
    agent["running"] = True
    agent["error"] = None
    try:
        saved = load_state(agent)
        if saved is not None:
            # Resume from what we've already handled: act only on files that are new
            # or changed since last time (including ones added while stopped).
            agent["base"] = saved
            note = f"Resumed from saved state ({len(saved)} file(s) known)."
        else:
            # First ever run: process_existing decides whether files already present
            # count as new. Either way, persist the baseline so restarts don't repeat.
            agent["base"] = {} if cfg["process_existing"] else _snap(agent)
            note = ("Acting on existing files first." if cfg["process_existing"]
                    else f"Baselined {len(agent['base'])} file(s).")
            save_state(agent)
        log(f"watching {agent['watch_dir']} (model={cfg['model']}, every {cfg['interval']}s"
            f"{', dry-run' if cfg['dry_run'] else ''}). " + note)
        while True:
            try:
                poll_once(agent, log)
            except Exception as e:                         # noqa: BLE001  (keep watching)
                agent["error"] = str(e)
                log(f"poll error: {e}")
            if once or (stop_event and stop_event.is_set()):
                return
            # Sleep in short slices so a stop is responsive.
            for _ in range(max(1, int(cfg["interval"] * 2))):
                if stop_event and stop_event.is_set():
                    return
                time.sleep(0.5)
    finally:
        agent["running"] = False
        if not once:
            log("stopped.")


def run_fleet(agents, once):
    if not agents:
        sys.exit("No agents to run.")
    threads = [threading.Thread(target=agent_loop, args=(a, once),
                                name=a["name"], daemon=not once) for a in agents]
    for t in threads:
        t.start()
    if once:
        for t in threads:
            t.join()
        return
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping…")                               # daemon threads exit with us


# --- supervisor (runtime control for the dashboard) -----------------------

class Supervisor:
    """Build agents from the registry and start/stop each in its own thread at
    runtime. Agent objects persist across stop/start so their status and recent
    activity remain visible in the dashboard."""

    def __init__(self, registry_path, cli_overrides=None):
        self.registry_path = registry_path
        self.cli = cli_overrides or {}
        self.agents = {}            # name -> agent dict
        self.enabled = {}           # name -> bool (from the registry)
        self.threads = {}           # name -> Thread
        self.stops = {}             # name -> Event
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        entries, defaults = load_registry(self.registry_path)
        for e in entries:
            name = e["name"]
            try:
                a = load_agent(os.path.join(HERE, name), name, reg_defaults=defaults,
                               entry_overrides=e["overrides"], cli_overrides=self.cli)
            except FileNotFoundError as err:               # keep a stub so it shows as broken
                a = {"name": name, "cfg": {}, "folder": os.path.join(HERE, name),
                     "watch_dir": "", "running": False, "error": str(err),
                     "actions": 0, "last_poll": None, "last_action": None, "recent": []}
            self.agents[name] = a
            self.enabled[name] = e["enabled"]

    def _reload_agent(self, name):
        """Re-read the registry + the agent's config.yaml/prompt from disk so edits
        made while it was stopped take effect, preserving the accumulated status
        (actions, recent log, last action/poll) the dashboard shows."""
        old = self.agents.get(name) or {}
        carry = {k: old.get(k) for k in ("actions", "last_action", "last_poll", "recent")}
        try:
            entries, defaults = load_registry(self.registry_path)
        except FileNotFoundError:
            entries, defaults = [], {}
        entry = next((e for e in entries if e["name"] == name), None)
        if entry is not None:
            self.enabled[name] = entry["enabled"]              # registry enable/disable too
        overrides = entry["overrides"] if entry else {}
        try:
            a = load_agent(os.path.join(HERE, name), name, reg_defaults=defaults,
                           entry_overrides=overrides, cli_overrides=self.cli)
        except FileNotFoundError as err:                       # config now missing/broken
            a = {"name": name, "cfg": {}, "folder": os.path.join(HERE, name),
                 "watch_dir": "", "running": False, "error": str(err),
                 "actions": 0, "last_poll": None, "last_action": None, "recent": []}
        a["actions"] = carry.get("actions") or 0
        a["last_action"] = carry.get("last_action")
        a["last_poll"] = carry.get("last_poll")
        a["recent"] = carry.get("recent") or []
        self.agents[name] = a
        return a

    def start(self, name):
        with self.lock:
            if name not in self.agents:
                return False                               # unknown
            if self.agents[name].get("running"):
                return False                               # already up
            a = self._reload_agent(name)                   # pick up on-disk config edits
            if not a.get("cfg"):
                return False                               # config missing/broken
            a["recent"].append(
                f"[{datetime.datetime.now():%H:%M:%S}] [{name}] config loaded from disk")
            del a["recent"][:-50]
            ev = threading.Event()
            t = threading.Thread(target=agent_loop, args=(a, False, ev),
                                 name=name, daemon=True)
            self.stops[name], self.threads[name] = ev, t
            t.start()
            return True

    def stop(self, name):
        with self.lock:
            ev = self.stops.get(name)
            if ev:
                ev.set()
            t = self.threads.get(name)
        if t:
            t.join(timeout=10)
        self.threads.pop(name, None)
        self.stops.pop(name, None)
        return True

    def start_enabled(self):
        for name, on in self.enabled.items():
            if on:
                self.start(name)

    def stop_all(self):
        for name in list(self.threads):
            self.stop(name)

    def status(self):
        out = []
        for name, a in self.agents.items():
            cfg = a.get("cfg") or {}
            out.append({
                "name": name, "enabled": self.enabled.get(name, True),
                "running": bool(a.get("running")), "acting": bool(a.get("acting")),
                "model": cfg.get("model"),
                "interval": cfg.get("interval"), "dry_run": cfg.get("dry_run"),
                "watch_dir": a.get("watch_dir"), "actions": a.get("actions", 0),
                "last_poll": a.get("last_poll"), "last_action": a.get("last_action"),
                "error": a.get("error"), "recent": (a.get("recent") or [])[-12:],
            })
        return out


# --- main -----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Lightweight folder-watching agents (Lumen).")
    ap.add_argument("--registry", default=os.path.join(HERE, "agents.yaml"),
                    help="agent registry (default: agents.yaml)")
    ap.add_argument("--agent", help="run only this agent folder (under agents/)")
    ap.add_argument("--config", help="run one explicit config.yaml (ad hoc)")
    ap.add_argument("--once", action="store_true", help="one pass per agent, then exit")
    ap.add_argument("--dry-run", action="store_true", help="assemble context, don't call Lumen")
    ap.add_argument("--interval", type=int, help="override poll seconds for all agents")
    ap.add_argument("--process-existing", action="store_true",
                    help="act on files already present, then keep watching")
    args = ap.parse_args()

    cli = {}
    if args.dry_run:
        cli["dry_run"] = True
    if args.process_existing:
        cli["process_existing"] = True
    if args.interval:
        cli["interval"] = args.interval

    agents = []
    try:
        if args.config:                                    # ad hoc: one config file
            folder = os.path.dirname(os.path.abspath(args.config))
            name = os.path.basename(folder) or "agent"
            agents = [_load_from_config(args.config, name, cli)]
        elif args.agent:                                   # one named agent folder
            agents = [load_agent(os.path.join(HERE, args.agent), args.agent,
                                 cli_overrides=cli)]
        else:                                              # the registry
            entries, defaults = load_registry(args.registry)
            for e in entries:
                if not e["enabled"]:
                    print(f"[skip] {e['name']} (disabled)")
                    continue
                agents.append(load_agent(os.path.join(HERE, e["name"]), e["name"],
                                         reg_defaults=defaults, entry_overrides=e["overrides"],
                                         cli_overrides=cli))
    except FileNotFoundError as e:
        sys.exit(str(e))

    print(f"Activating {len(agents)} agent(s): " + ", ".join(a["name"] for a in agents))
    run_fleet(agents, once=args.once)


def _load_from_config(cfg_path, name, cli_overrides):
    """Ad-hoc loader for --config: treat the file's folder as the agent folder."""
    folder = os.path.dirname(os.path.abspath(cfg_path))
    return load_agent(folder, name, cli_overrides=cli_overrides)


if __name__ == "__main__":
    main()

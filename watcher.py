#!/usr/bin/env python3
"""A lightweight folder-watching agent.

Polls a folder, and when changed files settle, sends their contents to Lumen using
Prompt.md as the instruction, then saves the model's response. Everything is driven
by config.yaml and Prompt.md in this folder.

    python3 watcher.py                 # watch using config.yaml
    python3 watcher.py --once          # one check-and-act pass, then exit
    python3 watcher.py --dry-run       # assemble + save context, never call Lumen
    python3 watcher.py --process-existing   # act on files already present, then watch

Change detection is by polling (snapshot of size+mtime), which is reliable over
network and Google Drive for Desktop mounts where OS file-watch events are not.
A changed file must look identical for `stable_polls` consecutive checks before it
is acted on, so a half-written or mid-sync file is not read prematurely.
"""
import os
import sys
import time
import argparse
import fnmatch
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


# --- config ---------------------------------------------------------------

def load_config(path):
    import yaml
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("watch_dir", "./inbox")
    cfg.setdefault("prompt", "Prompt.md")
    cfg.setdefault("model", "glm-5.3")
    cfg.setdefault("interval", 5)
    cfg.setdefault("stable_polls", 1)
    cfg.setdefault("events", ["added", "modified"])
    cfg.setdefault("recursive", True)
    cfg.setdefault("include", [])
    cfg.setdefault("exclude", [])
    cfg.setdefault("max_file_chars", 100000)
    cfg.setdefault("per_file", False)
    cfg.setdefault("process_existing", False)
    cfg.setdefault("output_dir", "responses")
    cfg.setdefault("dry_run", False)
    return cfg


def _abs(path):
    """Resolve a config path relative to this folder."""
    return path if os.path.isabs(path) else os.path.join(HERE, path)


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
        if os.path.realpath(root) == skip_abs or os.path.realpath(root).startswith(skip_abs + os.sep):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if not recursive:
            dirs[:] = []
        for name in files:
            if not _included(name, include, exclude):
                continue
            fp = os.path.join(root, name)
            try:
                st = os.stat(fp)                      # follows symlinks (Drive mounts)
            except OSError:
                continue
            snap[os.path.relpath(fp, folder)] = (st.st_size, st.st_mtime_ns)
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
            return None                              # binary: list by name only
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                txt = f.read()
    except Exception as e:                           # noqa: BLE001
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
        items = d.get(label, [])
        lines.append(label.capitalize() + ":")
        lines += ["  - " + p for p in items] or ["  (none)"]
    return "\n".join(lines)


def format_files(folder, rels, max_chars):
    blocks = []
    for rel in rels:
        text = extract_text(os.path.join(folder, rel), max_chars)
        if text is None:
            blocks.append(f"### {rel}\n[binary file — not read]")
        else:
            blocks.append(f"### {rel}\n{text}")
    return "\n\n".join(blocks) if blocks else "(no readable files)"


def build_messages(prompt_text, changes_text, files_text):
    user = (f"CHANGES:\n{changes_text}\n\n"
            f"FILES:\n{files_text}")
    return [{"role": "system", "content": prompt_text},
            {"role": "user", "content": user}]


_client = None
def lumen_client():
    global _client
    if _client is None:
        import lumen
        from openai import OpenAI
        _client = OpenAI(base_url=lumen.BASE_URL, api_key=lumen.api_key())
    return _client


def call_lumen(model, messages, timeout=900):
    c = lumen_client().with_options(max_retries=0, timeout=timeout)
    resp = c.chat.completions.create(model=model, messages=messages)
    return resp.choices[0].message.content.strip()


# --- acting on a settled batch --------------------------------------------

def save_response(out_dir, tag, context, response):
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, f"{stamp}{('-' + tag) if tag else ''}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Agent run {stamp}\n\n## Response\n\n{response}\n\n"
                f"---\n\n## Context sent\n\n{context}\n")
    return path


def act(cfg, folder, prompt_text, d, out_dir, log):
    events = cfg["events"]
    present = [p for lbl in ("added", "modified") if lbl in events for p in d.get(lbl, [])]
    changes_text = format_changes(d, events)
    if not present and "removed" not in events:
        return
    batches = ([[p] for p in present] if cfg["per_file"] and present else [present])
    for batch in batches:
        files_text = format_files(folder, batch, cfg["max_file_chars"])
        msgs = build_messages(prompt_text, changes_text, files_text)
        tag = (os.path.splitext(os.path.basename(batch[0]))[0] if cfg["per_file"] and batch else "")
        context = msgs[1]["content"]
        if cfg["dry_run"]:
            path = save_response(out_dir, tag, context, "(dry run — Lumen not called)")
            log(f"dry-run: {', '.join(batch) or 'removals'} -> {os.path.relpath(path, HERE)}")
            continue
        try:
            response = call_lumen(cfg["model"], msgs)
        except Exception as e:                       # noqa: BLE001
            log(f"ERROR calling Lumen: {e}")
            continue
        path = save_response(out_dir, tag, context, response)
        log(f"acted on {', '.join(batch) or 'removals'} -> {os.path.relpath(path, HERE)}")


# --- main loop ------------------------------------------------------------

def run(cfg, once=False):
    folder = _abs(cfg["watch_dir"])
    out_dir = _abs(cfg["output_dir"])
    prompt_path = _abs(cfg["prompt"])
    if not os.path.exists(prompt_path):
        sys.exit(f"Prompt file not found: {prompt_path}")
    with open(prompt_path, encoding="utf-8") as f:
        prompt_text = f.read().strip()
    os.makedirs(folder, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    skip_abs = os.path.realpath(out_dir)

    def log(msg):
        print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)

    snap = lambda: snapshot(folder, cfg["include"], cfg["exclude"],
                            cfg["recursive"], skip_abs)
    base = {} if cfg["process_existing"] else snap()
    log(f"watching {folder} (model={cfg['model']}, every {cfg['interval']}s"
        f"{', dry-run' if cfg['dry_run'] else ''}). "
        f"{'Acting on existing files first.' if cfg['process_existing'] else f'Baselined {len(base)} file(s).'}")

    prev_sig, stable_count = {}, {}
    while True:
        cur = snap()
        d = diff(base, cur)
        changed = d["added"] + d["modified"]
        for p in changed:
            stable_count[p] = stable_count.get(p, 0) + 1 if prev_sig.get(p) == cur.get(p) else 0
        prev_sig = cur
        settled = [p for p in changed if stable_count.get(p, 0) >= cfg["stable_polls"]]
        has_removed = bool(d["removed"]) and "removed" in cfg["events"]

        if settled or has_removed:
            d_settled = {"added": [p for p in d["added"] if p in settled],
                         "modified": [p for p in d["modified"] if p in settled],
                         "removed": d["removed"]}
            act(cfg, folder, prompt_text, d_settled, out_dir, log)
            base = cur                               # acknowledge handled changes
            stable_count.clear()
        elif changed:
            log(f"change detected, waiting to settle: {', '.join(changed)}")

        if once:
            return
        time.sleep(cfg["interval"])


def main():
    ap = argparse.ArgumentParser(description="Lightweight folder-watching agent (Lumen).")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--once", action="store_true", help="one check-and-act pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="assemble context but do not call Lumen")
    ap.add_argument("--process-existing", action="store_true",
                    help="act on files already present, then keep watching")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.dry_run:
        cfg["dry_run"] = True
    if args.process_existing:
        cfg["process_existing"] = True
    try:
        run(cfg, once=args.once)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()

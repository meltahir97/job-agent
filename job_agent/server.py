"""Local interactive web app — the site, but with working buttons.

GitHub Pages is static (no server to receive a click), so the action buttons
(Reject/Save a role, Approve/Dismiss a company) are served by this tiny localhost
app, which writes decisions straight to SQLite. Bound to 127.0.0.1 only — it's a
personal, single-user tool, so no auth. Stdlib only (no web framework).

    job-agent serve            # opens http://127.0.0.1:8765 in your browser

The page re-reads the DB on every load, so it always reflects the latest scored
roles; the scheduled pipeline keeps feeding it. Run it persistently via the
com.jobagent.serve launchd agent so the bookmark is always live.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

from . import db, discovery, store, website


# --- mutations (pure-ish; unit-tested without HTTP) -------------------------

def job_action(conn, job_id: int, action: str) -> Tuple[int, dict]:
    if not store.get_job(conn, job_id):
        return 404, {"ok": False, "error": f"no job with id {job_id}"}
    if action == "reject":
        store.record_feedback(conn, job_id, "dismissed")
    elif action == "save":
        store.record_feedback(conn, job_id, "saved")
    elif action == "undo":
        store.clear_feedback(conn, job_id)
    elif action == "applied":
        store.set_application(conn, job_id, "applied")
        store.record_feedback(conn, job_id, "saved", note="applied")  # strongest positive signal
    elif action == "unapply":
        store.clear_application(conn, job_id)
    else:
        return 400, {"ok": False, "error": f"unknown action {action!r}"}
    return 200, {"ok": True, "id": job_id, "action": action}


def application_update(conn, job_id: int, status: str) -> Tuple[int, dict]:
    if not store.get_application(conn, job_id):
        return 404, {"ok": False, "error": f"job {job_id} is not tracked as applied"}
    if status not in store.APP_STATUSES:
        return 400, {"ok": False, "error": f"unknown status {status!r}"}
    store.set_application(conn, job_id, status)
    return 200, {"ok": True, "id": job_id, "status": status}


def note_add(conn, job_id: int, body: dict) -> Tuple[int, dict]:
    if not store.get_job(conn, job_id):
        return 404, {"ok": False, "error": f"no job with id {job_id}"}
    text = (body.get("text") or "").strip()[:500]
    if not text:
        return 400, {"ok": False, "error": "empty note"}
    kind = body.get("kind") if body.get("kind") in ("note", "todo") else "note"
    nid = store.add_app_note(conn, job_id, text, kind)
    return 200, {"ok": True, "id": nid, "kind": kind}


def note_action(conn, note_id: int, action: str, body: dict) -> Tuple[int, dict]:
    if action == "toggle":
        store.set_note_done(conn, note_id, bool(body.get("done")))
    elif action == "delete":
        store.delete_app_note(conn, note_id)
    else:
        return 400, {"ok": False, "error": f"unknown action {action!r}"}
    return 200, {"ok": True, "id": note_id, "action": action}


def suggestion_action(conn, sid: int, action: str, ats=None, slug=None) -> Tuple[int, dict]:
    if not store.get_suggestion(conn, sid):
        return 404, {"ok": False, "error": f"no suggestion with id {sid}"}
    if action == "approve":
        msg = discovery.approve(conn, sid, ats=(ats or None), slug=(slug or None))
        ok = msg.startswith("approved")
        return (200 if ok else 422), {"ok": ok, "message": msg}
    if action == "dismiss":
        return 200, {"ok": True, "message": discovery.dismiss(conn, sid)}
    return 400, {"ok": False, "error": f"unknown action {action!r}"}


def draft_action(conn, job_id: int) -> Tuple[int, dict]:
    """Generate (or return existing) Drive drafts for ANY job — including roles not
    flagged as a match. Synchronous (one LLM call); fine under ThreadingHTTPServer.

    Never drafts the same role twice: an existing pair (by job id OR by company+title,
    catching re-fetched postings) is returned as-is; a local-only pair left over from a
    signed-out / storage-full moment is UPLOADED unchanged, not regenerated."""
    from . import drafting

    job = store.get_job(conn, job_id)
    if not job:
        return 404, {"ok": False, "error": f"no job with id {job_id}"}
    existing = (store.get_draft(conn, job_id)
                or store.get_draft_for_role(conn, job["company"] or "", job["title"] or ""))
    if existing and existing["drive_url"]:
        if existing["job_id"] != job_id:  # same role under a new id — link, don't redraft
            store.record_draft(conn, job_id, company=existing["company"], title=existing["title"],
                               dir=existing["dir"], drive_url=existing["drive_url"],
                               resume_url=existing["resume_url"], cover_url=existing["cover_url"],
                               model=existing["model"])
        return 200, {"ok": True, "folder": existing["drive_url"], "resume_url": existing["resume_url"],
                     "cover_url": existing["cover_url"], "existing": True}
    if existing:  # local-only pair: move the SAME files to Drive, no regeneration
        try:
            res = drafting.migrate_local_draft(conn, job, existing)
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "error": f"Couldn't upload the existing local draft to Drive: {e}"}
        if res:
            return 200, {"ok": True, "folder": res.get("folder"), "resume_url": res.get("resume_url"),
                         "cover_url": res.get("cover_url"), "where": res.get("where"), "migrated": True}
    try:
        master, voice = drafting.load_profiles()
    except FileNotFoundError as e:
        return 422, {"ok": False, "error": str(e)}
    try:
        # regenerate only needed when a stale record exists whose local files vanished
        res = drafting.generate_for_role(conn, job, master, voice, regenerate=bool(existing))
    except drafting.llm.LLMError as e:
        return 500, {"ok": False, "error": str(e)}
    return 200, {"ok": True, "folder": res.get("folder"), "resume_url": res.get("resume_url"),
                 "cover_url": res.get("cover_url"), "where": res.get("where")}


def render_page(conn, include_all: bool = False) -> str:
    rows = website.select_all_scored(conn) if include_all else website.select_master(conn)
    applied = store.applied_job_ids(conn)
    rows = [r for r in rows if r["id"] not in applied]  # applied roles live in their own section
    suggestions = store.list_suggestions(conn, "proposed")
    applications = store.list_applications(conn)
    notes = {a["id"]: store.list_app_notes(conn, a["id"]) for a in applications}
    page, _ = website.render_html(rows, suggestions=suggestions, interactive=True, include_all=include_all,
                                  applications=applications, app_notes=notes)
    return page


# --- HTTP -------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionError):
            pass  # client navigated away / refreshed mid-response — harmless

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            include_all = parse_qs(parsed.query).get("all", ["0"])[0] in ("1", "true", "yes")
            conn = db.connect()
            try:
                html = render_page(conn, include_all)
            finally:
                conn.close()
            self._send(200, html, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802
        parts = [p for p in self.path.split("?")[0].split("/") if p]  # api/job/12/reject
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        conn = db.connect()
        try:
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "job" and parts[3] == "draft":
                code, res = draft_action(conn, int(parts[2]))
            elif len(parts) == 4 and parts[0] == "api" and parts[1] == "job" and parts[3] == "status":
                code, res = application_update(conn, int(parts[2]), (body.get("status") or "").strip())
            elif len(parts) == 4 and parts[0] == "api" and parts[1] == "job" and parts[3] == "note":
                code, res = note_add(conn, int(parts[2]), body)
            elif len(parts) == 4 and parts[0] == "api" and parts[1] == "job":
                code, res = job_action(conn, int(parts[2]), parts[3])
            elif len(parts) == 4 and parts[0] == "api" and parts[1] == "note":
                code, res = note_action(conn, int(parts[2]), parts[3], body)
            elif len(parts) == 4 and parts[0] == "api" and parts[1] == "suggestion":
                code, res = suggestion_action(conn, int(parts[2]), parts[3], body.get("ats"), body.get("slug"))
            else:
                code, res = 404, {"ok": False, "error": "unknown endpoint"}
        except (ValueError, KeyError) as e:
            code, res = 400, {"ok": False, "error": str(e)}
        except Exception as e:  # never crash the server on one bad request
            code, res = 500, {"ok": False, "error": str(e)}
        finally:
            conn.close()
        self._send(code, json.dumps(res), "application/json")

    def log_message(self, *a):  # keep the console quiet
        return


def serve(port: int = 8765, open_browser: bool = True) -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"job-agent UI -> {url}   (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        httpd.shutdown()
    return 0

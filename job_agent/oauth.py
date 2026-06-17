"""User-delegated Google Drive access (OAuth) for WRITING drafts to your own Drive.

A service account can't own files on a personal Google account (no storage quota), so
generated drafts are written as the USER via OAuth: a one-time `job-agent auth` browser
consent stores a refresh token; thereafter the agent creates editable Google Docs in the
user's Drive (the token auto-refreshes). Scope is the minimal `drive.file` — the agent
can see/manage ONLY the files it creates, never the rest of your Drive.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Tuple

from . import config, db

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class OAuthError(RuntimeError):
    """Missing client secret / not yet authorized — callers fall back to local files."""


def is_authorized() -> bool:
    return config.GOOGLE_OAUTH_TOKEN_PATH.exists()


def _load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    tok = config.GOOGLE_OAUTH_TOKEN_PATH
    if not tok.exists():
        raise OAuthError("Not signed in to Google yet — run `job-agent auth` once.")
    creds = Credentials.from_authorized_user_file(str(tok), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            tok.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise OAuthError("Google sign-in expired — run `job-agent auth` again.")
    return creds


def user_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover
        raise OAuthError(f"Google libraries not installed: {e}") from e
    return build("drive", "v3", credentials=_load_credentials(), cache_discovery=False)


def run_auth_flow() -> str:
    """One-time browser consent. Saves the token; returns the signed-in email."""
    secret = config.GOOGLE_OAUTH_CLIENT_SECRET
    if not secret or not Path(secret).exists():
        raise OAuthError(
            "Set GOOGLE_OAUTH_CLIENT_SECRET in .env to your OAuth Desktop-client JSON "
            "(Cloud Console → APIs & Services → Credentials → Create OAuth client ID → Desktop app)."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(secret, SCOPES)
    creds = flow.run_local_server(
        port=0, prompt="consent", open_browser=True,
        authorization_prompt_message="Opening your browser to authorize Drive access…\nIf it doesn't open, visit: {url}",
    )
    config.GOOGLE_OAUTH_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.GOOGLE_OAUTH_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        from googleapiclient.discovery import build
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        return svc.about().get(fields="user(emailAddress)").execute().get("user", {}).get("emailAddress", "your account")
    except Exception:
        return "your account"


def _app_folder(conn, svc) -> str:
    """The user-owned 'Job Applications' folder (created once via OAuth; cached in meta)."""
    if config.GOOGLE_DRIVE_FOLDER_ID:
        return config.GOOGLE_DRIVE_FOLDER_ID
    cached = db.get_meta(conn, "drive_oauth_folder_id")
    if cached:
        try:
            svc.files().get(fileId=cached, fields="id").execute()
            return cached
        except Exception:
            pass
    folder = svc.files().create(
        body={"name": "Job Applications", "mimeType": FOLDER_MIME}, fields="id"
    ).execute()
    db.set_meta(conn, "drive_oauth_folder_id", folder["id"])
    return folder["id"]


def _subfolder(svc, parent_id: str, name: str) -> str:
    safe = name.replace("'", " ")
    found = svc.files().list(
        q=f"mimeType='{FOLDER_MIME}' and trashed=false and name='{safe}' and '{parent_id}' in parents",
        fields="files(id)", pageSize=1,
    ).execute().get("files", [])
    if found:
        return found[0]["id"]
    return svc.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}, fields="id"
    ).execute()["id"]


def _upload_doc(svc, name: str, docx_bytes: bytes, parent_id: str) -> Tuple[str, str]:
    """Upload a .docx, converting it to an editable Google Doc; replace same-named Doc."""
    from googleapiclient.http import MediaIoBaseUpload

    for f in svc.files().list(
        q=f"name='{name.replace(chr(39), ' ')}' and '{parent_id}' in parents and trashed=false",
        fields="files(id)", pageSize=10,
    ).execute().get("files", []):
        try:
            svc.files().delete(fileId=f["id"]).execute()
        except Exception:
            pass
    media = MediaIoBaseUpload(io.BytesIO(docx_bytes), mimetype=DOCX_MIME, resumable=False)
    created = svc.files().create(
        body={"name": name, "mimeType": GOOGLE_DOC, "parents": [parent_id]},
        media_body=media, fields="id,webViewLink",
    ).execute()
    return created["id"], created.get("webViewLink", "")


def upload_drafts(conn, company: str, title: str, resume_bytes: bytes, cover_bytes: bytes) -> dict:
    """Write resume + cover letter as editable Google Docs into the user's Drive.
    Returns {folder, resume_url, cover_url}. Raises OAuthError if not signed in."""
    svc = user_service()
    parent = _app_folder(conn, svc)
    sub = _subfolder(svc, parent, f"{company} — {title}"[:120])
    _, r_url = _upload_doc(svc, f"Resume — {company} — {title}"[:200], resume_bytes, sub)
    _, c_url = _upload_doc(svc, f"Cover Letter — {company} — {title}"[:200], cover_bytes, sub)
    return {"folder": f"https://drive.google.com/drive/folders/{sub}",
            "resume_url": r_url, "cover_url": c_url}

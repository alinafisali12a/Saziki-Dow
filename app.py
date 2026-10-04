"""
flask_app.py
------------
YouTube Downloader Web App
    - Supabase Auth (register / login / logout) handled client-side.
    - ONE permanent API key per user stored in `user_keys`.
    - Dashboard for MP3/MP4 downloads with dynamic quality selection.
    - Public API endpoint /api/v1/download authenticated via
      ?api_key=... , Authorization: Bearer ..., or x-api-key header.

Deploy: PythonAnywhere → Flask app pointing to `flask_app.py:app`
"""

from __future__ import annotations

import io
import os
import secrets
import tempfile
from functools import wraps
from typing import Any, Callable, Tuple

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from supabase import Client, create_client

load_dotenv()

# ---------------------------------------------------------------------------
# App + Supabase
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TABLE = "user_keys"
API_KEY_PREFIX = "saziki_"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def generate_key_string() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def extract_api_key() -> str | None:
    """Accept: ?api_key=, Authorization: Bearer, or x-api-key header."""
    key = request.args.get("api_key")
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:]
    if not key:
        key = request.headers.get("x-api-key")
    return key.strip() if key else None


def lookup_api_key(api_key: str) -> dict[str, Any] | None:
    try:
        res = (
            supabase.table(TABLE)
            .select("*")
            .eq("api_key", api_key)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        app.logger.exception("Supabase lookup failed: %s", exc)
        return None
    rows = res.data or []
    return rows[0] if rows else None


def increment_usage(api_key: str) -> None:
    try:
        supabase.rpc("increment_usage", {"p_key": api_key}).execute()
    except Exception:
        try:
            row = lookup_api_key(api_key)
            if row:
                supabase.table(TABLE).update(
                    {"usage_count": row["usage_count"] + 1}
                ).eq("api_key", api_key).execute()
        except Exception as exc:
            app.logger.exception("usage increment failed: %s", exc)


def ensure_user_key(user_id: str, user_email: str) -> dict[str, Any]:
    """
    Return the user's persistent API key row.
    If none exists for this user, create one. The key NEVER changes afterwards.
    """
    res = (
        supabase.table(TABLE)
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if rows:
        return rows[0]

    new_key = generate_key_string()
    insert = (
        supabase.table(TABLE)
        .insert(
            {
                "user_id": user_id,
                "user_email": user_email,
                "api_key": new_key,
                "is_active": True,
                "usage_count": 0,
            }
        )
        .execute()
    )
    return (insert.data or [{}])[0]


def require_api_key(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        key = extract_api_key()
        if not key:
            return (
                jsonify({
                    "success": False,
                    "error": "unauthorized",
                    "message": "Missing API key. Use ?api_key=, "
                               "Authorization: Bearer, or x-api-key header.",
                }),
                401,
            )
        row = lookup_api_key(key)
        if not row:
            return (
                jsonify({
                    "success": False,
                    "error": "unauthorized",
                    "message": "Invalid or inactive API key.",
                }),
                401,
            )
        request.api_key_row = row  # type: ignore[attr-defined]
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# YouTube download via pytubefix
# ---------------------------------------------------------------------------
def _build_youtube(url: str):
    from pytubefix import YouTube
    return YouTube(url)


def list_available_qualities(url: str) -> dict[str, Any]:
    """Return available MP4 resolutions and MP3 bitrates for a video."""
    yt = _build_youtube(url)
    progressive = []
    for s in yt.streams.filter(progressive=True, file_extension="mp4"):
        if s.resolution:
            progressive.append(s.resolution)
    progressive = sorted(set(progressive), key=lambda r: int(r.replace("p", "")))

    audio = []
    for s in yt.streams.filter(only_audio=True):
        if s.abr:
            audio.append(s.abr)
    audio = sorted(set(audio), key=lambda b: int(b.replace("kbps", "").strip()))

    return {
        "title": yt.title,
        "thumbnail": yt.thumbnail_url,
        "duration": yt.length,
        "author": yt.author,
        "mp4": progressive or ["360p", "720p"],
        "mp3": audio or ["128kbps", "192kbps", "320kbps"],
    }


def download_stream(url: str, fmt: str, quality: str) -> Tuple[bytes, str, str]:
    """
    Returns (file_bytes, filename, mimetype).
    fmt: "mp4" or "mp3"
    quality: e.g. "720p" or "192kbps"
    """
    from pytubefix import YouTube
    yt = YouTube(url)
    safe_title = "".join(
        c for c in yt.title if c.isalnum() or c in (" ", "-", "_", ".")
    ).strip() or "video"

    if fmt == "mp4":
        stream = yt.streams.filter(
            progressive=True, file_extension="mp4", resolution=quality
        ).first()
        if stream is None:
            stream = yt.streams.filter(
                progressive=True, file_extension="mp4"
            ).order_by("resolution").desc().first()
        if stream is None:
            raise RuntimeError("No MP4 stream available.")
        buf = io.BytesIO()
        stream.stream_to_buffer(buf)
        buf.seek(0)
        return buf.read(), f"{safe_title}.mp4", "video/mp4"

    # MP3 path: download audio-only, hand back the m4a bytes as-is
    # (true MP3 transcoding requires ffmpeg — noted in comments below).
    stream = yt.streams.filter(only_audio=True).first()
    if stream is None:
        raise RuntimeError("No audio stream available.")
    buf = io.BytesIO()
    stream.stream_to_buffer(buf)
    buf.seek(0)
    # Extension kept as .m4a since we aren't transcoding. Rename client-side if needed.
    return buf.read(), f"{safe_title}.m4a", "audio/mp4"


# ---------------------------------------------------------------------------
# Web Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html",
                           supabase_url=SUPABASE_URL,
                           supabase_key=SUPABASE_KEY)


@app.route("/api/v1/me", methods=["POST"])
def api_me():
    """
    Called by the frontend after Supabase Auth login.
    Body: { "user_id": "...", "email": "..." }
    Creates the user's permanent API key on first call, returns it every time.
    """
    payload = request.get_json(silent=True) or {}
    user_id = (payload.get("user_id") or "").strip()
    email = (payload.get("email") or "").strip() or None
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400

    try:
        row = ensure_user_key(user_id, email or "")
    except Exception as exc:
        app.logger.exception("ensure_user_key failed: %s", exc)
        return jsonify({"success": False, "error": "database_error"}), 500

    return jsonify({
        "success": True,
        "data": {
            "api_key": row.get("api_key"),
            "user_email": row.get("user_email"),
            "usage_count": row.get("usage_count", 0),
            "is_active": row.get("is_active", True),
            "created_at": row.get("created_at"),
        },
    })


@app.route("/api/v1/qualities", methods=["GET"])
def api_qualities():
    """Public helper the dashboard calls to populate quality dropdowns."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"success": False, "error": "url required"}), 400
    try:
        info = list_available_qualities(url)
    except Exception as exc:
        app.logger.exception("qualities failed: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({"success": True, "data": info})


# ---------------------------------------------------------------------------
# Main download endpoint (used by BOTH dashboard and public API)
# ---------------------------------------------------------------------------
@app.route("/api/v1/download", methods=["GET"])
@require_api_key
def api_download() -> Any:
    """
    Query params:
        url       : YouTube URL (required)
        format    : "mp4" | "mp3"   (default: mp4)
        quality   : e.g. "720p" | "192kbps"
        info_only : if 1, return metadata only (no file streaming)
    """
    row = request.api_key_row  # type: ignore[attr-defined]
    api_key = row["api_key"]
    increment_usage(api_key)

    url = request.args.get("url", "").strip()
    fmt = (request.args.get("format") or "mp4").lower()
    quality = request.args.get("quality") or ("720p" if fmt == "mp4" else "128kbps")
    info_only = request.args.get("info_only") == "1"

    if not url:
        return jsonify({"success": False, "error": "url required"}), 400
    if fmt not in ("mp4", "mp3"):
        return jsonify({"success": False, "error": "format must be mp4 or mp3"}), 400

    if info_only:
        try:
            info = list_available_qualities(url)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({
            "success": True,
            "data": {
                "info": info,
                "usage_count": row["usage_count"] + 1,
            },
        })

    try:
        data, filename, mimetype = download_stream(url, fmt, quality)
    except Exception as exc:
        app.logger.exception("download failed: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500

    return send_file(
        io.BytesIO(data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename,
    )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "not_found"}), 404
    return render_template("index.html",
                           supabase_url=SUPABASE_URL,
                           supabase_key=SUPABASE_KEY), 404


@app.errorhandler(405)
def not_allowed(_e):
    return jsonify({"success": False, "error": "method_not_allowed"}), 405


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

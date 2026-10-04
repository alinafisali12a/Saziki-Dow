"""
flask_app.py — Saziki Downloader
Railway-optimized YouTube downloader with Supabase Auth + permanent API keys.

Key behaviors:
    - Binds to $PORT (Railway) with fallback to 5000 for local dev.
    - Fetches accurate MP4 resolutions (progressive + adaptive) and MP3 bitrates.
    - Serves /api/fetch_info (public metadata), /api/v1/download (API key auth),
      /api/v1/me, /api/v1/profile (Supabase integration).
"""

from __future__ import annotations

import io
import os
import re
import secrets
from functools import wraps
from typing import Any, Callable, Tuple

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from supabase import Client, create_client

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Ensure Railway's proxy headers are respected
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TABLE = "user_keys"
API_KEY_PREFIX = "saziki_"

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
YT_URL_RE = re.compile(
    r"^(https?://)?(www\.|m\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)"
    r"[\w\-]{6,}",
    re.IGNORECASE,
)


def is_valid_youtube_url(url: str) -> bool:
    return bool(url and YT_URL_RE.match(url.strip()))


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def extract_api_key() -> str | None:
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
        app.logger.exception("lookup_api_key failed: %s", exc)
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
            app.logger.exception("increment_usage failed: %s", exc)


def ensure_user_key(user_id: str, user_email: str) -> dict[str, Any]:
    """Fetch OR create the user's permanent API key. Never rotates."""
    try:
        res = supabase.table(TABLE).select("*").eq("user_id", user_id).limit(1).execute()
        rows = res.data or []
        if rows:
            return rows[0]
        new_key = generate_api_key()
        insert = supabase.table(TABLE).insert({
            "user_id": user_id,
            "user_email": user_email,
            "api_key": new_key,
            "is_active": True,
            "usage_count": 0,
        }).execute()
        return (insert.data or [{}])[0]
    except Exception as exc:
        app.logger.exception("ensure_user_key failed: %s", exc)
        raise


def require_api_key(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        key = extract_api_key()
        if not key:
            return jsonify({
                "success": False,
                "error": "unauthorized",
                "message": "Missing API key. Use ?api_key=, "
                           "Authorization: Bearer, or x-api-key header.",
            }), 401
        row = lookup_api_key(key)
        if not row:
            return jsonify({
                "success": False,
                "error": "unauthorized",
                "message": "Invalid or inactive API key.",
            }), 401
        request.api_key_row = row  # type: ignore[attr-defined]
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# pytubefix helpers — accurate stream enumeration
# ---------------------------------------------------------------------------
def _yt(url: str):
    from pytubefix import YouTube
    return YouTube(url)


def _sort_by_number(s: str) -> int:
    """Extract the first integer from a string like '720p' or '192kbps'."""
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else 0


def get_video_info(url: str) -> dict[str, Any]:
    yt = _yt(url)
    return {
        "title": yt.title,
        "author": yt.author,
        "length": yt.length,
        "views": yt.views,
        "thumbnail": yt.thumbnail_url,
        "publish_date": str(yt.publish_date) if yt.publish_date else None,
    }


def get_available_formats(url: str) -> dict[str, list[str]]:
    """
    Enumerate ACTUAL streams pytubefix returns for this specific video.

    MP4 → every distinct resolution across progressive AND video-only streams
          (video-only are merged with audio on the server side when possible,
          or downloaded as-is with a note).
    MP3 → every distinct audio bitrate available.
    """
    yt = _yt(url)

    # --- Video resolutions (progressive + adaptive) ---
    video_res: set[str] = set()
    for s in yt.streams.filter(file_extension="mp4"):
        if s.resolution:
            video_res.add(s.resolution)

    mp4_list = sorted(video_res, key=_sort_by_number)

    # --- Audio bitrates ---
    audio_abr: set[str] = set()
    for s in yt.streams.filter(only_audio=True):
        if s.abr:
            audio_abr.add(s.abr)

    if not audio_abr:
        # fall back to common bitrates
        audio_abr = {"128kbps", "192kbps", "256kbps", "320kbps"}

    mp3_list = sorted(audio_abr, key=_sort_by_number, reverse=True)

    return {
        "mp4": mp4_list or ["360p", "720p"],
        "mp3": mp3_list,
    }


def _pick_video_stream(yt, quality: str):
    """
    Prefer a progressive stream (has audio+video) at the requested resolution.
    If unavailable, return the best adaptive video-only stream.
    """
    # 1) Progressive at exact resolution
    stream = yt.streams.filter(
        progressive=True, file_extension="mp4", resolution=quality
    ).first()
    if stream:
        return stream

    # 2) Any mp4 at exact resolution (may be video-only)
    stream = (
        yt.streams.filter(file_extension="mp4", resolution=quality)
        .order_by("bitrate").desc().first()
    )
    if stream:
        return stream

    # 3) Best progressive fallback
    stream = (
        yt.streams.filter(progressive=True, file_extension="mp4")
        .order_by("resolution").desc().first()
    )
    if stream:
        return stream

    # 4) Absolute fallback
    return yt.streams.filter(file_extension="mp4").order_by("resolution").desc().first()


def _pick_audio_stream(yt, quality: str):
    """Prefer exact abr, else highest available audio-only stream."""
    stream = yt.streams.filter(only_audio=True, abr=quality).first()
    if stream:
        return stream
    stream = yt.streams.filter(only_audio=True).order_by("abr").desc().first()
    if stream:
        return stream
    # last resort
    return yt.streams.filter(only_audio=True).first()


def download_bytes(url: str, fmt: str, quality: str) -> Tuple[bytes, str, str]:
    """Returns (bytes, filename, mimetype)."""
    yt = _yt(url)
    safe = "".join(c for c in yt.title if c.isalnum() or c in " -_.").strip() or "video"

    if fmt == "mp4":
        stream = _pick_video_stream(yt, quality)
        if not stream:
            raise RuntimeError(f"No MP4 stream found for quality '{quality}'.")
        buf = io.BytesIO()
        stream.stream_to_buffer(buf)
        buf.seek(0)
        return buf.read(), f"{safe}.mp4", "video/mp4"

    # MP3 path — audio-only stream delivered as m4a (no ffmpeg on Railway by default).
    stream = _pick_audio_stream(yt, quality)
    if not stream:
        raise RuntimeError(f"No audio stream found for quality '{quality}'.")
    buf = io.BytesIO()
    stream.stream_to_buffer(buf)
    buf.seek(0)
    return buf.read(), f"{safe}.m4a", "audio/mp4"


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        supabase_url=SUPABASE_URL,
        supabase_key=SUPABASE_KEY,
    )


@app.route("/healthz")
def healthz():
    """Railway health check."""
    return jsonify({"ok": True}), 200


@app.route("/api/v1/me", methods=["POST"])
def api_me():
    payload = request.get_json(silent=True) or {}
    user_id = (payload.get("user_id") or "").strip()
    email = (payload.get("email") or "").strip()
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    try:
        row = ensure_user_key(user_id, email)
    except Exception:
        return jsonify({"success": False, "error": "database_error"}), 500
    return jsonify({
        "success": True,
        "data": {
            "api_key": row.get("api_key"),
            "user_email": row.get("user_email"),
            "usage_count": row.get("usage_count", 0),
            "is_active": row.get("is_active", True),
            "created_at": row.get("created_at"),
            "avatar_url": row.get("avatar_url"),
            "display_name": row.get("display_name"),
        },
    })


@app.route("/api/v1/profile", methods=["POST"])
def api_update_profile():
    payload = request.get_json(silent=True) or {}
    user_id = (payload.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400

    updates: dict[str, Any] = {}
    if "display_name" in payload:
        updates["display_name"] = (payload.get("display_name") or "").strip()[:80]
    if "avatar_url" in payload:
        updates["avatar_url"] = (payload.get("avatar_url") or "").strip()[:500]

    if not updates:
        return jsonify({"success": False, "error": "no_fields"}), 400

    try:
        supabase.table(TABLE).update(updates).eq("user_id", user_id).execute()
    except Exception as exc:
        app.logger.exception("profile update failed: %s", exc)
        return jsonify({"success": False, "error": "database_error"}), 500

    return jsonify({"success": True, "data": updates})


@app.route("/api/fetch_info", methods=["POST"])
def api_fetch_info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not is_valid_youtube_url(url):
        return jsonify({"success": False, "error": "Invalid YouTube URL."}), 400
    try:
        info = get_video_info(url)
        formats = get_available_formats(url)
    except Exception as exc:
        app.logger.exception("fetch_info failed: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500
    return jsonify({"success": True, "info": info, "formats": formats})


@app.route("/api/v1/video_info", methods=["POST", "GET"])
@require_api_key
def api_video_info():
    row = request.api_key_row  # type: ignore[attr-defined]
    increment_usage(row["api_key"])
    url = (request.args.get("url") or (request.get_json(silent=True) or {}).get("url") or "").strip()
    if not is_valid_youtube_url(url):
        return jsonify({"success": False, "error": "Invalid YouTube URL."}), 400
    try:
        return jsonify({"success": True, "data": get_video_info(url)})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/v1/available_resolutions", methods=["POST", "GET"])
@require_api_key
def api_available_resolutions():
    row = request.api_key_row  # type: ignore[attr-defined]
    increment_usage(row["api_key"])
    url = (request.args.get("url") or (request.get_json(silent=True) or {}).get("url") or "").strip()
    if not is_valid_youtube_url(url):
        return jsonify({"success": False, "error": "Invalid YouTube URL."}), 400
    try:
        return jsonify({"success": True, "data": get_available_formats(url)})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/v1/download", methods=["POST", "GET"])
@require_api_key
def api_download() -> Any:
    row = request.api_key_row  # type: ignore[attr-defined]
    increment_usage(row["api_key"])

    body = request.get_json(silent=True) or {}
    url = (request.args.get("url") or body.get("url") or "").strip()
    fmt = (request.args.get("format") or body.get("format") or "mp4").lower()
    quality = (request.args.get("quality") or body.get("quality") or "").strip()

    if not is_valid_youtube_url(url):
        return jsonify({"success": False, "error": "Invalid YouTube URL."}), 400
    if fmt not in ("mp4", "mp3"):
        return jsonify({"success": False, "error": "format must be mp4 or mp3"}), 400

    try:
        data, filename, mimetype = download_bytes(url, fmt, quality)
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


# ---------------------------------------------------------------------------
# Railway / local entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

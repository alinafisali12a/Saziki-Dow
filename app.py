"""
app.py
------
Flask Web API + Supabase integration.

Phase 2 Responsibilities:
    - Validate API keys against the Supabase `api_keys` table.
    - Increment usage_count atomically on every successful call.
    - Provide a POST /api/v1/generate_key route to issue new test keys.
    - Keep the public homepage route.

Environment variables (see .env):
    SUPABASE_URL : Supabase project URL
    SUPABASE_KEY : Supabase anon (or service_role) API key
    SECRET_KEY   : Flask session secret
"""

from __future__ import annotations

import os
import secrets
from functools import wraps
from typing import Any, Callable, Tuple

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from supabase import Client, create_client

# ---------------------------------------------------------------------------
# Environment & App Setup
# ---------------------------------------------------------------------------
load_dotenv()  # reads .env into os.environ (no-op in production if unset)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_KEY must be set (see .env / environment)."
    )

# Single shared client instance — thread-safe for our use case.
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Constants
API_KEY_HEADER = "x-api-key"
API_KEY_QUERY = "api_key"
API_KEY_PREFIX = "saziki_"
TABLE = "api_keys"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_api_key() -> str | None:
    """Read the API key from header `x-api-key` or `?api_key=` query param."""
    key = request.headers.get(API_KEY_HEADER) or request.args.get(API_KEY_QUERY)
    return key.strip() if key else None


def lookup_api_key(api_key: str) -> dict[str, Any] | None:
    """
    Fetch a single row from Supabase for the given key.

    Returns the row as a dict if found AND is_active = True, else None.
    """
    try:
        response = (
            supabase.table(TABLE)
            .select("id, key, user_email, is_active, usage_count, created_at")
            .eq("key", api_key)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # network / auth / etc.
        app.logger.exception("Supabase lookup failed: %s", exc)
        return None

    rows = response.data or []
    return rows[0] if rows else None


def increment_usage(api_key: str) -> None:
    """
    Increment usage_count atomically via the `increment_usage` RPC function
    defined in the SQL setup script. Falls back to a manual update if the
    RPC is unavailable.
    """
    try:
        supabase.rpc("increment_usage", {"p_key": api_key}).execute()
    except Exception as exc:
        app.logger.warning("RPC increment failed, falling back: %s", exc)
        try:
            row = lookup_api_key(api_key)
            if row:
                supabase.table(TABLE).update(
                    {"usage_count": row["usage_count"] + 1}
                ).eq("key", api_key).execute()
        except Exception as exc2:
            app.logger.exception("Fallback usage increment failed: %s", exc2)


def generate_key_string() -> str:
    """Generate a URL-safe key with the `saziki_` prefix."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def require_api_key(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator: enforce a valid, active API key present in Supabase.

    On success, attaches the matched row to `request.api_key_row`.
    On failure, returns 401 with a JSON error payload.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        api_key = extract_api_key()

        if not api_key:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "unauthorized",
                        "message": (
                            f"Missing API key. Provide it via the "
                            f"'{API_KEY_HEADER}' header or '?{API_KEY_QUERY}=' query parameter."
                        ),
                    }
                ),
                401,
            )

        row = lookup_api_key(api_key)
        if not row:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "unauthorized",
                        "message": "Invalid or inactive API key.",
                    }
                ),
                401,
            )

        # Attach to request context for downstream handlers.
        request.api_key_row = row  # type: ignore[attr-defined]
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def homepage() -> str:
    """Simple HTML landing page so the service is visibly alive."""
    return """
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <title>Web API &amp; Dashboard</title>
        <style>
          body {
            font-family: system-ui, -apple-system, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
          }
          .card {
            background: #1e293b;
            padding: 2.5rem 3rem;
            border-radius: 12px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.4);
            text-align: center;
            max-width: 520px;
          }
          h1 { margin-top: 0; color: #38bdf8; }
          code {
            background: #0f172a;
            padding: 2px 6px;
            border-radius: 4px;
            color: #fbbf24;
          }
        </style>
      </head>
      <body>
        <div class="card">
          <h1>🚀 Web API &amp; Dashboard</h1>
          <p>Service online. Supabase-backed API key auth is active.</p>
          <p>
            <code>GET  /api/v1/download?api_key=YOUR_KEY</code><br />
            <code>POST /api/v1/generate_key</code>
          </p>
        </div>
      </body>
    </html>
    """


@app.route("/api/v1/download", methods=["GET"])
@require_api_key
def api_download() -> Tuple[Any, int]:
    """
    Protected download endpoint.

    Validates the API key against Supabase, increments usage, and returns
    a confirmation payload. Actual downloader logic arrives in Phase 3.
    """
    row = request.api_key_row  # type: ignore[attr-defined]
    api_key = row["key"]

    # Atomically bump usage count.
    increment_usage(api_key)

    target_url = request.args.get("url")
    output_format = request.args.get("format", "mp4")

    return (
        jsonify(
            {
                "success": True,
                "message": "Authenticated against Supabase. Downloader pending Phase 3.",
                "data": {
                    "api_key_preview": api_key[:12] + "...",
                    "user_email": row.get("user_email"),
                    "usage_count_before": row["usage_count"],
                    "usage_count_after": row["usage_count"] + 1,
                    "requested_url": target_url,
                    "requested_format": output_format,
                    "status": "queued",
                },
            }
        ),
        200,
    )


@app.route("/api/v1/generate_key", methods=["POST"])
def generate_key() -> Tuple[Any, int]:
    """
    Issue a new API key and persist it in Supabase.

    Body (JSON, all optional):
        { "user_email": "user@example.com" }

    Returns the newly created key. In production, gate this behind
    admin auth or a signup flow — right now it's open for testing.
    """
    payload = request.get_json(silent=True) or {}
    user_email = (payload.get("user_email") or "").strip() or None

    new_key = generate_key_string()

    try:
        response = (
            supabase.table(TABLE)
            .insert(
                {
                    "key": new_key,
                    "user_email": user_email,
                    "is_active": True,
                    "usage_count": 0,
                }
            )
            .execute()
        )
    except Exception as exc:
        app.logger.exception("Failed to insert new API key: %s", exc)
        return (
            jsonify(
                {
                    "success": False,
                    "error": "database_error",
                    "message": "Could not create API key. Please try again.",
                }
            ),
            500,
        )

    created = (response.data or [{}])[0]

    return (
        jsonify(
            {
                "success": True,
                "message": "API key generated successfully.",
                "data": {
                    "api_key": created.get("key", new_key),
                    "user_email": created.get("user_email"),
                    "is_active": created.get("is_active", True),
                    "usage_count": created.get("usage_count", 0),
                    "created_at": created.get("created_at"),
                },
            }
        ),
        201,
    )


# ---------------------------------------------------------------------------
# Error Handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_err: Any) -> Tuple[Any, int]:
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "not_found"}), 404
    return "<h1>404 — Page Not Found</h1>", 404


@app.errorhandler(405)
def method_not_allowed(_err: Any) -> Tuple[Any, int]:
    return jsonify({"success": False, "error": "method_not_allowed"}), 405


# ---------------------------------------------------------------------------
# Dev Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Production: gunicorn -w 4 -b 0.0.0.0:5000 app:app
    app.run(host="0.0.0.0", port=5000, debug=True)

"""
app.py
------
Core Flask application entry point for the Web API & Dashboard service.

Phase 1 Responsibilities:
    - Initialize the Flask app.
    - Expose a public homepage (/).
    - Expose a protected API endpoint (/api/v1/download) that requires an API key.
    - Run on 0.0.0.0:5000 (development).

Notes:
    - API key validation is stubbed for Phase 1 (any non-empty key is accepted).
    - Real key issuance/validation will come in a later phase (via a database).
"""

from __future__ import annotations

import os
from functools import wraps
from typing import Any, Callable, Tuple

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# App Initialization
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Configurable via environment variable, defaults to a dev placeholder.
# In production, this value must be a strong random secret.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Header name + query param name clients can use to authenticate.
API_KEY_HEADER = "x-api-key"
API_KEY_QUERY = "api_key"


# ---------------------------------------------------------------------------
# API Key Extraction & Validation Helpers
# ---------------------------------------------------------------------------
def extract_api_key() -> str | None:
    """
    Pull the API key from either the request header (`x-api-key`)
    or the query string (`?api_key=...`).

    Header is preferred over query string when both are present.
    """
    key = request.headers.get(API_KEY_HEADER)
    if not key:
        key = request.args.get(API_KEY_QUERY)
    return key.strip() if key else None


def validate_api_key(api_key: str | None) -> bool:
    """
    Phase 1 stub validation.

    Any non-empty string is considered a valid key. This will be replaced
    with a database / cache lookup in a later phase.
    """
    return bool(api_key)


def require_api_key(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator that enforces API key presence on a route.

    If the key is missing (or fails validation), returns 401 Unauthorized
    with a JSON error body. Otherwise, the wrapped view is called.
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

        if not validate_api_key(api_key):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "unauthorized",
                        "message": "Invalid API key.",
                    }
                ),
                401,
            )

        # Attach the key to the request context for downstream handlers.
        request.api_key = api_key  # type: ignore[attr-defined]
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def homepage() -> str:
    """
    Simple HTML landing page so the service is visibly alive at the root URL.
    Will be replaced by a dashboard in a later phase.
    """
    return """
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <title>Web API & Dashboard</title>
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
            max-width: 480px;
          }
          h1 { margin-top: 0; color: #38bdf8; }
          code {
            background: #0f172a;
            padding: 2px 6px;
            border-radius: 4px;
            color: #fbbf24;
          }
          a { color: #38bdf8; }
        </style>
      </head>
      <body>
        <div class="card">
          <h1>🚀 Web API &amp; Dashboard</h1>
          <p>Welcome! The service is up and running.</p>
          <p>
            Try the API endpoint:<br />
            <code>GET /api/v1/download?api_key=YOUR_KEY</code>
          </p>
        </div>
      </body>
    </html>
    """


@app.route("/api/v1/download", methods=["GET"])
@require_api_key
def api_download() -> Tuple[Any, int]:
    """
    Phase 1 dummy endpoint.

    Accepts requests only when a valid API key is supplied.
    Returns a placeholder JSON payload so clients can integrate
    before the real downloader is wired up.

    Query parameters (future use):
        url  : target video URL
        fmt  : desired output format (e.g. "mp4", "mp3")
    """
    # These params are not used yet, but validated so the contract is clear.
    target_url = request.args.get("url")
    output_format = request.args.get("format", "mp4")

    return (
        jsonify(
            {
                "success": True,
                "message": "API key accepted. Downloader not yet implemented (Phase 1).",
                "data": {
                    "api_key_preview": request.api_key[:6] + "..." if len(request.api_key) > 6 else "***",  # type: ignore[attr-defined]
                    "requested_url": target_url,
                    "requested_format": output_format,
                    "status": "queued",
                },
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# Error Handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_err: Any) -> Tuple[Any, int]:
    """Return JSON (not HTML) for unknown API routes; HTML for others."""
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
    # NOTE: For production, run via Gunicorn instead:
    #   gunicorn -w 4 -b 0.0.0.0:5000 app:app
    app.run(host="0.0.0.0", port=5000, debug=True)

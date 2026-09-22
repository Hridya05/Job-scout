import csv
import io
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, Response
from werkzeug.exceptions import HTTPException

from .adapters import validate_source
from .domain import STATUSES, validate_preferences
from .scanner import Scanner
from .storage import Store


def create_app(data_path=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.config["TRUSTED_HOSTS"] = ["localhost", "127.0.0.1", "[::1]"]
    store = Store(data_path or Path(__file__).resolve().parent.parent / "data" / "job-scout.sqlite3")
    scanner = Scanner(store)
    app.extensions.update(store=store, scanner=scanner)

    @app.before_request
    def local_requests_only():
        if request.remote_addr not in {"127.0.0.1", "::1", None}:
            return jsonify(error="This application accepts local connections only."), 403
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("X-Job-Scout") != "1":
                return jsonify(error="Missing local application request header. Refresh the app."), 403
            origin = request.headers.get("Origin")
            if origin:
                parts = urlsplit(origin)
                if parts.netloc != request.host or parts.scheme != request.scheme:
                    return jsonify(error="Cross-origin changes are not allowed."), 403
            if not request.is_json:
                return jsonify(error="Send changes as application/json."), 415

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def validation_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict_error(error):
        app.logger.warning("Database constraint: %s", error)
        return jsonify(error="That source already exists, or the referenced record no longer exists."), 409

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.exception("Request failed")
        return jsonify(error="The request failed. See the app console for details."), 500

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        return jsonify(
            preferences=store.preferences(), sources=store.sources(),
            scan=scanner.state(), counts=store.counts(),
        )

    @app.put("/api/preferences")
    def preferences():
        value = validate_preferences(request.get_json())
        store.save_preferences(value)
        return jsonify(value)

    @app.post("/api/scan")
    def scan():
        try:
            scanner.start()
        except ValueError as error:
            return jsonify(error=str(error)), 409
        return jsonify(message="Scan started. Source status updates as each source finishes."), 202

    @app.post("/api/sources")
    def add_source():
        with scanner.lock:
            if scanner.running:
                return jsonify(error="Wait for the current scan before editing sources."), 409
            return jsonify(store.save_source(validate_source(request.get_json()))), 201

    @app.patch("/api/sources/<int:identifier>")
    def edit_source(identifier):
        with scanner.lock:
            if scanner.running:
                return jsonify(error="Wait for the current scan before editing sources."), 409
            current = store.source(identifier)
            if current is None:
                return jsonify(error="Source not found."), 404
            return jsonify(store.save_source(validate_source(request.get_json(), current), identifier))

    @app.delete("/api/sources/<int:identifier>")
    def delete_source(identifier):
        with scanner.lock:
            if scanner.running:
                return jsonify(error="Wait for the current scan before deleting sources."), 409
            if not store.delete_source(identifier):
                return jsonify(error="Source not found."), 404
            return jsonify(message="Source and its associated jobs were deleted.")

    def selected_jobs():
        view = request.args.get("view", "matches")
        if view not in {"matches", "all", "saved", "applied"}:
            raise ValueError("Unknown job view.")
        query = request.args.get("q", "").strip()
        if len(query) > 200:
            raise ValueError("Search must contain at most 200 characters.")
        return store.filtered_jobs(view, query)

    @app.get("/api/jobs")
    def jobs():
        try:
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 50))
        except ValueError as error:
            raise ValueError("Offset and limit must be integers.") from error
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Offset must be nonnegative and limit must be 1-200.")
        values = selected_jobs()
        return jsonify(jobs=values[offset:offset + limit], total=len(values))

    @app.patch("/api/jobs/<int:identifier>")
    def update_job(identifier):
        changes = request.get_json()
        if not isinstance(changes, dict) or not changes or set(changes) - {"status", "application_notes"}:
            raise ValueError("Supply a status and/or application_notes.")
        if "status" in changes and (
            not isinstance(changes["status"], str) or changes["status"] not in STATUSES
        ):
            raise ValueError("Unknown application status.")
        if "application_notes" in changes and (
            not isinstance(changes["application_notes"], str) or len(changes["application_notes"]) > 10000
        ):
            raise ValueError("Application notes must be text of at most 10000 characters.")
        if not store.update_job(identifier, changes):
            return jsonify(error="Job not found."), 404
        return jsonify(message="Job updated.")

    @app.get("/api/export")
    def export():
        buffer = io.StringIO(newline="")
        fields = ["title", "company", "locations", "url", "status", "applied_at", "application_notes", "first_seen", "last_seen"]
        writer = csv.writer(buffer)
        writer.writerow(fields)
        for job in selected_jobs():
            values = []
            for field in fields:
                value = "; ".join(job[field]) if isinstance(job[field], list) else str(job[field] or "")
                if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
                    value = "'" + value
                values.append(value)
            writer.writerow(values)
        return Response(
            "\ufeff" + buffer.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="job-scout-export.csv"'},
        )

    return app

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3

from .domain import APPLICATION_STATUSES, DEFAULT_PREFERENCES, matches_preferences, now_iso


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_scan TEXT, last_error TEXT, last_warning TEXT,
                    last_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(url, kind)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    source_key TEXT NOT NULL, external_id TEXT NOT NULL,
                    title TEXT NOT NULL, company TEXT NOT NULL,
                    locations TEXT NOT NULL, url TEXT NOT NULL, description TEXT NOT NULL,
                    posted_at TEXT, remote INTEGER NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    application_notes TEXT NOT NULL DEFAULT '', applied_at TEXT,
                    UNIQUE(source_id, source_key, external_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
                    finished_at TEXT, error TEXT
                );
            """)
            if db.execute("SELECT 1 FROM settings WHERE key='preferences'").fetchone() is None:
                db.execute(
                    "INSERT INTO settings VALUES ('preferences', ?)",
                    (json.dumps(DEFAULT_PREFERENCES),),
                )
                db.executemany(
                    "INSERT INTO sources(name,url,kind) VALUES(?,?,?)",
                    [
                        ("Google", "https://www.google.com/about/careers/applications/jobs/results/", "google"),
                        ("JPMorgan Chase", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs", "oracle"),
                    ],
                )
            db.execute(
                "UPDATE scans SET finished_at=?, error=? WHERE finished_at IS NULL",
                (now_iso(), "The app stopped before this scan completed. Run a new scan."),
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def preferences(self):
        with self.connect() as db:
            return json.loads(db.execute("SELECT value FROM settings WHERE key='preferences'").fetchone()[0])

    def save_preferences(self, value):
        with self.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE key='preferences'", (json.dumps(value),))

    @staticmethod
    def _source(row):
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def sources(self):
        with self.connect() as db:
            return [self._source(row) for row in db.execute("SELECT * FROM sources ORDER BY id")]

    def source(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (identifier,)).fetchone()
            return self._source(row) if row else None

    def save_source(self, value, identifier=None):
        with self.connect() as db:
            values = (value["name"], value["url"], value["kind"], int(value["enabled"]))
            if identifier is None:
                identifier = db.execute(
                    "INSERT INTO sources(name,url,kind,enabled) VALUES(?,?,?,?)", values
                ).lastrowid
            else:
                db.execute(
                    "UPDATE sources SET name=?,url=?,kind=?,enabled=?,last_error=NULL,last_warning=NULL WHERE id=?",
                    (*values, identifier),
                )
        return self.source(identifier)

    def delete_source(self, identifier):
        with self.connect() as db:
            return db.execute("DELETE FROM sources WHERE id=?", (identifier,)).rowcount

    def save_jobs(self, source, jobs, warnings):
        timestamp = now_iso()
        source_key = hashlib.sha256((source["kind"] + "|" + source["url"]).encode()).hexdigest()
        unique = {job.external_id: job for job in jobs}
        with self.connect() as db:
            for job in unique.values():
                values = asdict(job)
                values.update(
                    source_id=source["id"], source_key=source_key,
                    locations=json.dumps(job.locations), first_seen=timestamp, last_seen=timestamp,
                )
                db.execute("""
                    INSERT INTO jobs(source_id,source_key,external_id,title,company,locations,url,
                                     description,posted_at,remote,first_seen,last_seen)
                    VALUES(:source_id,:source_key,:external_id,:title,:company,:locations,:url,
                           :description,:posted_at,:remote,:first_seen,:last_seen)
                    ON CONFLICT(source_id,source_key,external_id) DO UPDATE SET
                        title=excluded.title, company=excluded.company, locations=excluded.locations,
                        url=excluded.url, description=excluded.description,
                        posted_at=excluded.posted_at, remote=excluded.remote, last_seen=excluded.last_seen
                """, values)
            db.execute(
                "UPDATE sources SET last_scan=?,last_error=NULL,last_warning=?,last_count=? WHERE id=?",
                (timestamp, "\n".join(dict.fromkeys(warnings)) or None, len(unique), source["id"]),
            )

    def source_error(self, identifier, error):
        with self.connect() as db:
            db.execute("UPDATE sources SET last_scan=?,last_error=? WHERE id=?", (now_iso(), error, identifier))

    def jobs(self):
        with self.connect() as db:
            rows = db.execute("""
                SELECT jobs.*, sources.name AS source_name FROM jobs
                JOIN sources ON sources.id=jobs.source_id
                ORDER BY jobs.first_seen DESC, jobs.id DESC
            """).fetchall()
        preferences = self.preferences()
        result = []
        for row in rows:
            job = dict(row)
            job["locations"] = json.loads(job["locations"])
            job["remote"] = bool(job["remote"])
            job["matches"] = matches_preferences(job, preferences)
            result.append(job)
        return result

    def filtered_jobs(self, view="matches", query=""):
        jobs = self.jobs()
        if view == "matches":
            jobs = [job for job in jobs if job["matches"] and job["status"] != "dismissed"]
        elif view == "saved":
            jobs = [job for job in jobs if job["status"] == "saved"]
        elif view == "applied":
            jobs = [job for job in jobs if job["status"] in APPLICATION_STATUSES]
        if query:
            query = query.casefold()
            jobs = [job for job in jobs if query in " ".join([
                job["title"], job["company"], job["source_name"], *job["locations"],
                job["description"], job["application_notes"],
            ]).casefold()]
        return jobs

    def counts(self):
        jobs = self.jobs()
        return {
            "total": len(jobs),
            "matches": sum(job["matches"] and job["status"] != "dismissed" for job in jobs),
            "saved": sum(job["status"] == "saved" for job in jobs),
            "applied": sum(job["status"] in APPLICATION_STATUSES for job in jobs),
        }

    def update_job(self, identifier, updates):
        with self.connect() as db:
            row = db.execute("SELECT status,application_notes,applied_at FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return False
            status = updates.get("status", row["status"])
            notes = updates.get("application_notes", row["application_notes"])
            applied_at = row["applied_at"]
            if status in APPLICATION_STATUSES and applied_at is None:
                applied_at = now_iso()
            db.execute(
                "UPDATE jobs SET status=?,application_notes=?,applied_at=? WHERE id=?",
                (status, notes, applied_at, identifier),
            )
            return True

    def start_scan(self):
        with self.connect() as db:
            return db.execute("INSERT INTO scans(started_at) VALUES(?)", (now_iso(),)).lastrowid

    def finish_scan(self, identifier, error):
        with self.connect() as db:
            db.execute("UPDATE scans SET finished_at=?,error=? WHERE id=?", (now_iso(), error, identifier))

    def last_scan(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

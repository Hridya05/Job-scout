from pathlib import Path
import tempfile
import unittest

from job_scout.app import create_app
from job_scout.domain import DEFAULT_PREFERENCES, Job
from job_scout.storage import Store


HEADERS = {"X-Job-Scout": "1"}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "jobs.sqlite3"
        self.app = create_app(self.path)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions["store"]
        self.source = self.store.sources()[0]
        self.store.save_jobs(self.source, [Job(
            "one", "Software Engineer", "Example", ["India"], "https://example.com/job/one", "Use Python"
        )], [])
        self.identifier = self.store.jobs()[0]["id"]

    def tearDown(self):
        self.directory.cleanup()

    def change(self, path, payload, method="patch"):
        return getattr(self.client, method)(path, json=payload, headers=HEADERS)

    def test_state_and_preferences(self):
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["counts"]["total"], 1)
        self.assertEqual(len(state["sources"]), 2)
        preferences = {**DEFAULT_PREFERENCES, "keywords": ["designer"]}
        self.assertEqual(self.change("/api/preferences", preferences, "put").status_code, 200)
        self.assertEqual(self.client.get("/api/jobs").get_json()["total"], 0)
        self.assertEqual(self.client.get("/api/jobs?view=all").get_json()["total"], 1)

    def test_status_notes_and_first_applied_date_survive_rescan(self):
        path = f"/api/jobs/{self.identifier}"
        self.assertEqual(self.change(path, {"status": "applied", "application_notes": "Referral from team"}).status_code, 200)
        first = self.store.jobs()[0]
        self.assertIsNotNone(first["applied_at"])
        self.store.save_jobs(self.source, [Job(
            "one", "Updated Engineer", "Example", ["India"], "https://example.com/job/one"
        )], [])
        self.change(path, {"status": "interviewing"})
        job = self.client.get("/api/jobs?view=applied&q=referral").get_json()["jobs"][0]
        self.assertEqual(job["status"], "interviewing")
        self.assertEqual(job["application_notes"], "Referral from team")
        self.assertEqual(job["applied_at"], first["applied_at"])
        self.assertEqual(job["first_seen"], first["first_seen"])
        self.assertEqual(job["title"], "Updated Engineer")
        self.assertEqual(self.client.get("/api/state").get_json()["counts"]["applied"], 1)

    def test_note_only_edit_does_not_set_applied_date(self):
        self.change(f"/api/jobs/{self.identifier}", {"application_notes": "Read later"})
        self.assertIsNone(self.store.jobs()[0]["applied_at"])

    def test_dismissed_hidden_from_matches_not_all(self):
        self.change(f"/api/jobs/{self.identifier}", {"status": "dismissed"})
        self.assertEqual(self.client.get("/api/jobs").get_json()["total"], 0)
        self.assertEqual(self.client.get("/api/jobs?view=all").get_json()["total"], 1)

    def test_duplicate_jobs_upsert(self):
        job = Job("one", "Updated", "Example", [], "https://example.com/job/one")
        self.store.save_jobs(self.source, [job, job], ["A warning"])
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.source(self.source["id"])["last_count"], 1)

    def test_source_edit_keeps_previous_tracking_and_separates_new_ids(self):
        self.change(f"/api/jobs/{self.identifier}", {"status": "saved"})
        value = self.change("/api/sources", {
            "name": "Other", "url": "https://example.com/jobs", "kind": "auto",
        }, "post").get_json()
        self.assertEqual(value["kind"], "jsonld")
        source_id = value["id"]
        job = Job("one", "First company", "Example", [], "https://example.com/one")
        self.store.save_jobs(value, [job], [])
        response = self.change(f"/api/sources/{source_id}", {"url": "https://second.example/jobs"})
        self.store.save_jobs(response.get_json(), [Job("one", "Other company", "Other", [], "https://second.example/one")], [])
        self.assertEqual(len(self.store.jobs()), 3)
        self.assertEqual(self.client.get("/api/jobs?view=saved").get_json()["total"], 1)

    def test_source_deletion_and_no_reseed(self):
        for source in self.store.sources():
            self.assertEqual(self.change(f"/api/sources/{source['id']}", {}, "delete").status_code, 200)
        restored = Store(self.path)
        self.assertEqual(restored.sources(), [])
        self.assertEqual(restored.jobs(), [])

    def test_duplicate_source_conflict(self):
        response = self.change("/api/sources", {
            "name": "Again", "url": self.source["url"], "kind": "google",
        }, "post")
        self.assertEqual(response.status_code, 409)

    def test_bad_inputs_and_not_found(self):
        for payload in [{"status": "unknown"}, {"status": []}, {"application_notes": None}, {}, {"extra": 1}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.change(f"/api/jobs/{self.identifier}", payload).status_code, 400)
        self.assertEqual(self.change("/api/jobs/9999", {"status": "saved"}).status_code, 404)
        self.assertEqual(self.client.get("/api/jobs?offset=-1").status_code, 400)
        self.assertEqual(self.client.get("/api/jobs?limit=no").status_code, 400)
        self.assertEqual(self.client.get("/api/jobs?view=unknown").status_code, 400)

    def test_mutations_require_header_and_same_origin(self):
        self.assertEqual(self.client.patch(f"/api/jobs/{self.identifier}", json={"status": "saved"}).status_code, 403)
        response = self.client.patch(
            f"/api/jobs/{self.identifier}", json={"status": "saved"},
            headers={**HEADERS, "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={"Host": "evil.example"}).status_code, 400)

    def test_sources_locked_during_scan(self):
        self.app.extensions["scanner"].running = True
        self.assertEqual(self.change(f"/api/sources/{self.source['id']}", {"enabled": False}).status_code, 409)
        self.assertEqual(self.change(f"/api/sources/{self.source['id']}", {}, "delete").status_code, 409)

    def test_csv_tracks_filters_and_escapes_formulas(self):
        self.change(f"/api/jobs/{self.identifier}", {"status": "applied", "application_notes": "=HYPERLINK(\"bad\")"})
        response = self.client.get("/api/export?view=applied")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("Software Engineer", text)
        self.assertNotIn("Software Engineer", self.client.get("/api/export?view=saved").get_data(as_text=True))

    def test_reopen_persists_preferences_and_tracking(self):
        self.change(f"/api/jobs/{self.identifier}", {"status": "offer", "application_notes": "Decision next week"})
        self.change("/api/preferences", {**DEFAULT_PREFERENCES, "locations": ["India"]}, "put")
        restored = Store(self.path)
        self.assertEqual(restored.jobs()[0]["status"], "offer")
        self.assertEqual(restored.preferences()["locations"], ["India"])

    def test_unfinished_scan_reported_after_restart(self):
        self.store.start_scan()
        restored = Store(self.path)
        self.assertIn("stopped", restored.last_scan()["error"])

from datetime import datetime, timedelta
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from job_scout.domain import FetchResult, Job, SourceError
from job_scout.http import Fetcher, public_url
from job_scout.scanner import Scanner
from job_scout.storage import Store


def response(text, status=200, url="https://example.com/jobs", headers=None):
    value = requests.Response()
    value.status_code = status
    value.url = url
    value._content = text.encode()
    value._content_consumed = True
    value.encoding = "utf-8"
    value.headers.update(headers or {})
    return value


class HttpTests(unittest.TestCase):
    def test_private_dns_answers_blocked(self):
        for address in ["127.0.0.1", "192.168.1.1", "169.254.169.254", "::1"]:
            with self.subTest(address=address), patch("job_scout.http.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            ]), self.assertRaises(SourceError):
                public_url("https://example.com")

    def test_google_style_wildcard_rules(self):
        client = Fetcher()
        client.session.get = Mock(return_value=response(
            "User-agent: *\nDisallow: /jobs?*&page=\nDisallow: /jobs?page=\n"
        ))
        with patch("job_scout.http.public_url", side_effect=lambda url: url), patch("job_scout.http.time.sleep"):
            with self.assertRaisesRegex(SourceError, "disallowed"):
                client.get("https://example.com/jobs?q=python&page=2")
        self.assertEqual(client.session.get.call_count, 1)
        client.close()

    def test_robots_unavailable_403_is_visible_and_listing_remains_public(self):
        client = Fetcher()
        client.session.get = Mock(side_effect=[response("", 403), response("jobs")])
        with patch("job_scout.http.public_url", side_effect=lambda url: url), patch("job_scout.http.time.sleep"):
            self.assertEqual(client.get("https://example.com/jobs").text, "jobs")
        self.assertIn("403", client.warnings[0])
        client.close()

    def test_robots_rate_limit_defers(self):
        client = Fetcher()
        client.session.get = Mock(return_value=response("", 429))
        with patch("job_scout.http.public_url", side_effect=lambda url: url), self.assertRaisesRegex(SourceError, "deferred"):
            client.get("https://example.com/jobs")
        self.assertEqual(client.session.get.call_count, 1)
        client.close()

    def test_listing_access_denial_is_not_bypassed(self):
        client = Fetcher()
        client.session.get = Mock(side_effect=[response("", 404), response("Blocked", 403)])
        with patch("job_scout.http.public_url", side_effect=lambda url: url), patch("job_scout.http.time.sleep"):
            with self.assertRaisesRegex(SourceError, "HTTP 403"):
                client.get("https://example.com/jobs")
        client.close()

    def test_redirect_destination_checked(self):
        client = Fetcher()
        client.session.get = Mock(return_value=response("", 302, headers={"Location": "http://127.0.0.1/"}))
        with patch("job_scout.http.public_url", side_effect=["https://example.com/robots.txt", SourceError("private")]):
            with self.assertRaisesRegex(SourceError, "private"):
                client._download("https://example.com/robots.txt", check_robots=False)
        self.assertEqual(client.session.get.call_count, 1)
        client.close()

    def test_response_size_bounded(self):
        client = Fetcher()
        client.session.get = Mock(return_value=response("x" * 100))
        with patch("job_scout.http.public_url", side_effect=lambda url: url):
            with self.assertRaisesRegex(SourceError, "size limit"):
                client._download("https://example.com/jobs", check_robots=False, max_bytes=50)
        client.close()


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "test.sqlite3")
        self.client = Mock(warnings=[])
        self.scanner = Scanner(self.store, client_factory=lambda: self.client)

    def tearDown(self):
        self.scanner.stop()
        self.directory.cleanup()

    def test_failed_source_does_not_erase_history_or_stop_others(self):
        source = self.store.sources()[0]
        self.store.save_jobs(source, [Job("old", "Saved Engineer", "Google", [], "https://example.com/old")], [])
        self.store.update_job(self.store.jobs()[0]["id"], {"status": "applied"})
        good = FetchResult([Job("new", "Bank Engineer", "Bank", [], "https://example.com/new")], ["Limited coverage"])
        identifier = self.store.start_scan()
        self.scanner.running = True
        with patch.dict("job_scout.scanner.ADAPTERS", {
            "google": Mock(side_effect=SourceError("Blocked by site")),
            "oracle": Mock(return_value=good),
        }):
            self.scanner._run(identifier, self.store.sources(), self.store.preferences())
        self.assertFalse(self.scanner.running)
        self.assertEqual(len(self.store.jobs()), 2)
        self.assertEqual(self.store.counts()["applied"], 1)
        self.assertIn("Blocked", self.store.sources()[0]["last_error"])
        self.assertIn("Limited", self.store.sources()[1]["last_warning"])
        self.assertIn("Google", self.store.last_scan()["error"])

    def test_manual_default_has_no_schedule(self):
        self.assertIsNone(self.scanner.next_run())
        self.store.save_preferences({**self.store.preferences(), "scan_interval_minutes": 60})
        self.assertIsNotNone(self.scanner.next_run())

    def test_schedule_uses_last_completion_and_pauses_while_running(self):
        self.store.save_preferences({**self.store.preferences(), "scan_interval_minutes": 30})
        identifier = self.store.start_scan()
        self.store.finish_scan(identifier, None)
        finished = datetime.fromisoformat(self.store.last_scan()["finished_at"])
        self.assertEqual(self.scanner.next_run(), finished + timedelta(minutes=30))
        self.scanner.running = True
        self.assertIsNone(self.scanner.next_run())

    def test_overlapping_scans_rejected(self):
        with patch("job_scout.scanner.threading.Thread.start"):
            self.scanner.start()
            with self.assertRaisesRegex(ValueError, "already running"):
                self.scanner.start()

    def test_all_disabled_prevents_scan_and_schedule(self):
        for source in self.store.sources():
            self.store.save_source({**source, "enabled": False}, source["id"])
        self.store.save_preferences({**self.store.preferences(), "scan_interval_minutes": 60})
        with self.assertRaisesRegex(ValueError, "Enable"):
            self.scanner.start()
        self.assertIsNone(self.scanner.next_run())

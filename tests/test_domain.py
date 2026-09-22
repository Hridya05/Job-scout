from copy import deepcopy
import unittest

from job_scout.adapters import validate_source
from job_scout.domain import DEFAULT_PREFERENCES, matches_preferences, validate_preferences, validate_url


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.preferences = deepcopy(DEFAULT_PREFERENCES)
        self.job = {
            "title": "Software Engineer", "description": "Build Python services with C++.",
            "locations": ["Bengaluru, Karnataka, India"], "remote": False,
        }

    def test_empty_filters_match_everything(self):
        self.assertTrue(matches_preferences(self.job, self.preferences))

    def test_any_keyword_and_any_location_but_both_groups_required(self):
        self.preferences.update(keywords=["designer", "Python"], locations=["London", "India"])
        self.assertTrue(matches_preferences(self.job, self.preferences))
        self.preferences["locations"] = ["Canada"]
        self.assertFalse(matches_preferences(self.job, self.preferences))

    def test_keyword_boundaries_and_punctuation(self):
        self.preferences["keywords"] = ["C++"]
        self.assertTrue(matches_preferences(self.job, self.preferences))
        self.preferences["keywords"] = ["Go"]
        self.job["description"] = "Google services"
        self.assertFalse(matches_preferences(self.job, self.preferences))

    def test_exclusions_only_apply_to_title(self):
        self.preferences["exclude_keywords"] = ["senior"]
        self.job["description"] = "Collaborate with senior engineers."
        self.assertTrue(matches_preferences(self.job, self.preferences))
        self.job["title"] = "Senior Software Engineer"
        self.assertFalse(matches_preferences(self.job, self.preferences))

    def test_remote_must_be_explicit(self):
        self.preferences["remote_only"] = True
        self.job["description"] = "Use remote debugging."
        self.assertFalse(matches_preferences(self.job, self.preferences))
        self.job["remote"] = True
        self.assertTrue(matches_preferences(self.job, self.preferences))

    def test_invalid_preferences(self):
        for key, value in [
            ("keywords", "python"), ("keywords", [""] * 6), ("locations", ["x"] * 4),
            ("remote_only", 1), ("max_pages", True), ("max_pages", 11),
            ("scan_interval_minutes", 5), ("exclude_keywords", ["x\n"]),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_preferences({**self.preferences, key: value})
        with self.assertRaises(ValueError):
            validate_preferences({})

    def test_validation_trims_and_deduplicates(self):
        value = validate_preferences({**self.preferences, "keywords": [" Python ", "Python"]})
        self.assertEqual(value["keywords"], ["Python"])


class SourceValidationTests(unittest.TestCase):
    def test_source_detection(self):
        value = validate_source({"name": "Google", "url": "https://careers.google.com/jobs/"})
        self.assertEqual(value["kind"], "google")
        value = validate_source({
            "name": "Bank", "url": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
        })
        self.assertEqual(value["kind"], "oracle")
        value = validate_source({"name": "Other", "url": "https://example.com/jobs"})
        self.assertEqual(value["kind"], "jsonld")

    def test_invalid_adapter_hosts(self):
        for kind in ["google", "oracle", "unknown", [], None]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_source({"name": "Wrong", "url": "https://example.com/jobs", "kind": kind})

    def test_url_rejects_credentials_and_dangerous_schemes(self):
        for url in ["javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com",
                    "https://example.com:8000", "https://example.com\\@localhost", None]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)

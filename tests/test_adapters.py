import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from job_scout.adapters import google, jsonld, oracle
from job_scout.domain import DEFAULT_PREFERENCES, SourceError


def google_html(records, total):
    return (
        '<a href="jobs/results/123-software-engineer?q=python">View</a>'
        '<script class="ds:1">AF_initDataCallback({key: \'ds:1\', data:'
        + json.dumps([records, None, total, 20]) + ", sideChannel: {}});</script>"
    )


class GoogleTests(unittest.TestCase):
    def test_embedded_jobs(self):
        row = ["123", "Software Engineer", None, [None, "<p>Build things.</p>"],
               [None, "<p>Python</p>"], None, None, "Google", "en-US",
               [["Bengaluru, India"]], [None, "<p>Work here.</p>"]]
        jobs, total = google.parse_jobs(google_html([row], 15))
        self.assertEqual(total, 15)
        self.assertEqual(jobs[0].locations, ["Bengaluru, India"])
        self.assertEqual(jobs[0].description, "Build things. Python Work here.")
        self.assertEqual(jobs[0].url, google.BASE + "jobs/results/123-software-engineer")
        self.assertIsNone(jobs[0].posted_at)

    def test_valid_empty_result(self):
        self.assertEqual(google.parse_jobs(google_html(None, 0)), ([], 0))

    def test_changed_markup_is_an_error(self):
        for html in ["<html>Challenge</html>", google_html(None, 12)]:
            with self.subTest(html=html), self.assertRaises(SourceError):
                google.parse_jobs(html)

    def test_search_all_query_combinations_without_pagination(self):
        client = Mock()
        client.get.return_value.text = google_html([], 0)
        preferences = {**DEFAULT_PREFERENCES, "keywords": ["Python", "designer"], "locations": ["India", "Canada"]}
        google.fetch({"url": google.BASE + "jobs/results/"}, preferences, client)
        self.assertEqual(client.get.call_count, 4)
        self.assertTrue(all("page=" not in call.args[0] for call in client.get.call_args_list))


class OracleTests(unittest.TestCase):
    source = {"name": "Bank", "url": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"}

    def payload(self, identifiers, total):
        return {"items": [{"TotalJobsCount": total, "requisitionList": [
            {"Id": value, "Title": "Python Engineer", "PrimaryLocation": "India",
             "ShortDescriptionStr": "<p>Build APIs</p>", "PostedDate": "2026-09-01"}
            for value in identifiers
        ]}]}

    def test_parse_and_pagination(self):
        client = Mock()
        client.get.side_effect = [
            SimpleNamespace(json=lambda: self.payload(["1", "2"], 3)),
            SimpleNamespace(json=lambda: self.payload(["3"], 3)),
        ]
        preferences = {**DEFAULT_PREFERENCES, "keywords": ["Python"], "locations": ["India"]}
        result = oracle.fetch(self.source, preferences, client)
        self.assertEqual(len(result.jobs), 3)
        self.assertEqual(result.warnings, [])
        second = parse_qs(urlsplit(client.get.call_args_list[1].args[0]).query)["finder"][0]
        self.assertIn("offset=25", second)
        self.assertIn("location=India", second)
        self.assertTrue(result.jobs[0].url.endswith("/sites/CX_1001/job/1"))

    def test_cap_warning(self):
        client = Mock()
        client.get.return_value.json.return_value = self.payload(["1"], 100)
        result = oracle.fetch(self.source, {**DEFAULT_PREFERENCES, "max_pages": 1}, client)
        self.assertIn("1 of 100", result.warnings[0])

    def test_repeated_page_fails(self):
        client = Mock()
        client.get.return_value.json.return_value = self.payload(["1"], 100)
        with self.assertRaisesRegex(SourceError, "repeated"):
            oracle.fetch(self.source, DEFAULT_PREFERENCES, client)

    def test_genuine_zero_results(self):
        client = Mock()
        client.get.return_value.json.return_value = self.payload([], 0)
        self.assertEqual(oracle.fetch(self.source, DEFAULT_PREFERENCES, client).jobs, [])

    def test_malformed_shape_does_not_look_empty(self):
        with self.assertRaises(SourceError):
            oracle.parse_jobs({"items": []}, self.source)

    def test_finder_delimiters_rejected(self):
        with self.assertRaisesRegex(SourceError, "commas or semicolons"):
            oracle.fetch(self.source, {**DEFAULT_PREFERENCES, "keywords": ["x;limit=999"]}, Mock())


class JsonLdTests(unittest.TestCase):
    def html(self, value):
        return '<script type="application/ld+json">' + json.dumps(value).replace("<", "\\u003c") + "</script>"

    def test_graph_remote_and_location_restrictions(self):
        value = {"@graph": [{"@type": "Organization", "name": "Company"}, {
            "@type": ["Thing", "JobPosting"], "title": "Engineer", "url": "/jobs/1",
            "description": "<p>Build</p><script>bad()</script>", "identifier": {"value": "one"},
            "hiringOrganization": {"name": "Example"}, "jobLocationType": "TELECOMMUTE",
            "applicantLocationRequirements": {"@type": "Country", "name": "India"},
        }]}
        jobs, next_url = jsonld.parse_jobs(self.html(value), "https://example.com/careers", "Source")
        self.assertIsNone(next_url)
        self.assertEqual(jobs[0].url, "https://example.com/jobs/1")
        self.assertEqual(jobs[0].locations, ["India"])
        self.assertTrue(jobs[0].remote)
        self.assertEqual(jobs[0].description, "Build")

    def test_nested_item_list_and_relative_next(self):
        value = {"@type": "ItemList", "itemListElement": [{"item": {
            "@type": "JobPosting", "title": "Designer",
            "jobLocation": {"address": {"addressLocality": "London", "addressCountry": {"name": "UK"}}},
        }}]}
        jobs, next_url = jsonld.parse_jobs(
            self.html(value) + '<a rel="next" href="?page=2">Next</a>', "https://example.com/jobs", "Source"
        )
        self.assertEqual(jobs[0].locations, ["London, UK"])
        self.assertEqual(next_url, "https://example.com/jobs?page=2")

    def test_no_job_schema_fails_explicitly(self):
        client = Mock()
        client.get.return_value = SimpleNamespace(text="<p>Loading...</p>", url="https://example.com")
        with self.assertRaisesRegex(SourceError, "No structured"):
            jsonld.fetch({"url": "https://example.com", "name": "Example"}, DEFAULT_PREFERENCES, client)

    def test_dangerous_job_link_rejected(self):
        with self.assertRaises(SourceError):
            jsonld.parse_jobs(self.html({
                "@type": "JobPosting", "title": "Bad", "url": "javascript:alert(1)",
            }), "https://example.com", "Source")

    def test_same_site_next_page_is_followed(self):
        client = Mock()
        client.get.side_effect = [
            SimpleNamespace(
                text=self.html({"@type": "JobPosting", "title": "First", "url": "/jobs/1"})
                + '<a rel="next" href="?page=2">Next</a>',
                url="https://example.com/jobs",
            ),
            SimpleNamespace(
                text=self.html({"@type": "JobPosting", "title": "Second", "url": "/jobs/2"}),
                url="https://example.com/jobs?page=2",
            ),
        ]
        result = jsonld.fetch({"name": "Example", "url": "https://example.com/jobs"}, DEFAULT_PREFERENCES, client)
        self.assertEqual([job.title for job in result.jobs], ["First", "Second"])
        self.assertEqual(result.warnings, [])

    def test_cross_site_next_page_is_not_followed(self):
        client = Mock()
        client.get.return_value = SimpleNamespace(
            text=self.html({"@type": "JobPosting", "title": "First"})
            + '<a rel="next" href="https://other.example/jobs">Next</a>',
            url="https://example.com/jobs",
        )
        result = jsonld.fetch({"name": "Example", "url": "https://example.com/jobs"}, DEFAULT_PREFERENCES, client)
        self.assertEqual(client.get.call_count, 1)
        self.assertIn("not followed", result.warnings[0])

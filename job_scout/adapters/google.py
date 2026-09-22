import json
import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..domain import FetchResult, Job, SourceError, search_queries
from .common import plain_text


BASE = "https://www.google.com/about/careers/applications/"


def parse_jobs(html):
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", class_="ds:1")
    if script is None or not script.string:
        raise SourceError("Google's job data was not found. Its page format may have changed.")
    marker = re.search(r"\bdata:\s*", script.string)
    if not marker:
        raise SourceError("Google's embedded job data has an unsupported format.")
    try:
        data, _ = json.JSONDecoder().raw_decode(script.string[marker.end():])
        if not isinstance(data, list) or len(data) < 3 or type(data[2]) is not int:
            raise ValueError("missing result count")
        if data[0] is None and data[2] == 0:
            return [], 0
        if not isinstance(data[0], list) or (not data[0] and data[2] > 0):
            raise ValueError("missing job records")
        links = {}
        for anchor in soup.find_all("a", href=True):
            match = re.search(r"(?:^|/)jobs/results/(\d+)(?:-|/|$|\?)", anchor["href"])
            if match:
                parts = urlsplit(urljoin(BASE, anchor["href"]))
                links[match[1]] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        jobs = []
        for row in data[0]:
            if not isinstance(row, list) or len(row) < 11 or not isinstance(row[1], str):
                raise ValueError("invalid job record")
            identifier = str(row[0])
            if not identifier.isdigit():
                raise ValueError("invalid job identifier")
            locations = [location[0] for location in row[9] or []]
            description = " ".join(
                plain_text(row[index][1])
                for index in (3, 4, 10)
                if row[index] and len(row[index]) > 1 and row[index][1]
            )
            jobs.append(Job(
                external_id=identifier,
                title=row[1],
                company=row[7] or "Google",
                locations=locations,
                url=links.get(identifier, BASE + "jobs/results/" + identifier),
                description=description,
                remote=any(re.search(r"\bremote\b", value, re.I) for value in locations),
            ))
        return jobs, data[2]
    except (ValueError, TypeError, IndexError, KeyError) as error:
        raise SourceError(f"Google's job data could not be parsed: {error}") from error


def fetch(source, preferences, client):
    result = FetchResult()
    for keyword, location in search_queries(preferences):
        params = {key: value for key, value in (("q", keyword), ("location", location)) if value}
        jobs, total = parse_jobs(client.get(source["url"] + "?" + urlencode(params)).text)
        result.jobs.extend(jobs)
        if total > len(jobs):
            result.warnings.append(
                f"Google returned {len(jobs)} of {total} results for "
                f"'{keyword or 'all roles'}' / '{location or 'all locations'}'. "
                "Only the first page is scanned because Google restricts pagination in robots.txt. "
                "Narrow your search for better coverage."
            )
    return result

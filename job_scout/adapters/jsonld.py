import hashlib
import json
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..domain import FetchResult, Job, SourceError, validate_url
from .common import plain_text


def job_nodes(value):
    if isinstance(value, list):
        for child in value:
            yield from job_nodes(child)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
            yield value
        else:
            for child in value.values():
                if isinstance(child, (dict, list)):
                    yield from job_nodes(child)


def parse_jobs(html, url, company):
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except ValueError as error:
            raise SourceError("The page contains invalid JSON-LD.") from error
        for node in job_nodes(data):
            try:
                title = node["title"]
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("missing title")
                job_url = validate_url(urljoin(url, node.get("url") or url))
                identifier = node.get("identifier")
                if isinstance(identifier, dict):
                    identifier = identifier.get("value")
                if not isinstance(identifier, (str, int)) or not str(identifier):
                    identifier = hashlib.sha256((job_url + "|" + title).encode()).hexdigest()
                organization = node.get("hiringOrganization") or {}
                locations = node.get("jobLocation") or []
                if not isinstance(locations, list):
                    locations = [locations]
                names = []
                for location in locations:
                    address = location.get("address", {})
                    if isinstance(address, str):
                        names.append(address)
                        continue
                    country = address.get("addressCountry", "")
                    if isinstance(country, dict):
                        country = country.get("name", "")
                    names.append(", ".join(str(value) for value in (
                        address.get("addressLocality"), address.get("addressRegion"), country
                    ) if value))
                remote = node.get("jobLocationType") == "TELECOMMUTE"
                if remote:
                    restrictions = node.get("applicantLocationRequirements") or []
                    if not isinstance(restrictions, list):
                        restrictions = [restrictions]
                    names.extend(item["name"] for item in restrictions if item.get("name"))
                    if not names:
                        names.append("Remote")
                jobs.append(Job(
                    external_id=str(identifier),
                    title=title,
                    company=organization.get("name") or company,
                    locations=[name for name in names if name],
                    url=job_url,
                    description=plain_text(node.get("description")),
                    posted_at=node.get("datePosted"),
                    remote=remote,
                ))
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise SourceError(f"A structured job posting could not be parsed: {error}") from error
    next_link = soup.find("a", rel=lambda value: value and "next" in value)
    return jobs, urljoin(url, next_link["href"]) if next_link and next_link.get("href") else None


def fetch(source, preferences, client):
    result = FetchResult()
    url = source["url"]
    seen = set()
    for _ in range(preferences["max_pages"]):
        if url in seen:
            raise SourceError("The page's next link loops back to an already scanned page.")
        seen.add(url)
        response = client.get(url)
        jobs, next_url = parse_jobs(response.text, response.url, source["name"])
        if not jobs:
            raise SourceError(
                "No structured JobPosting data was found. Use a page with JSON-LD job listings "
                "or add a dedicated adapter for this site; JavaScript-only pages are not supported."
            )
        result.jobs.extend(jobs)
        if not next_url:
            return result
        if urlsplit(next_url).netloc != urlsplit(response.url).netloc:
            result.warnings.append("A next-page link left the source website and was not followed.")
            return result
        url = next_url
    result.warnings.append("The page limit was reached; additional pages were not scanned.")
    return result

import re
from urllib.parse import urlencode, urlsplit

from ..domain import FetchResult, Job, SourceError, search_queries
from .common import plain_text


PAGE_SIZE = 25


def parse_jobs(payload, source):
    try:
        group = payload["items"][0]
        rows, total = group["requisitionList"], group["TotalJobsCount"]
        if not isinstance(rows, list) or type(total) is not int:
            raise ValueError("invalid result list or count")
        parts = urlsplit(source["url"])
        site_path = re.search(r"(.*/sites/[A-Za-z0-9_-]+)", parts.path)[1]
        base = f"{parts.scheme}://{parts.netloc}{site_path}"
        jobs = []
        for row in rows:
            if not row.get("Id") or not isinstance(row.get("Title"), str) or not row["Title"].strip():
                raise ValueError("job is missing an ID or title")
            location = row.get("PrimaryLocation") or ""
            jobs.append(Job(
                external_id=str(row["Id"]),
                title=row["Title"],
                company=source["name"],
                locations=[location] if location else [],
                url=base + "/job/" + str(row["Id"]),
                description=" ".join(
                    plain_text(row.get(key))
                    for key in ("ShortDescriptionStr", "ExternalQualificationsStr", "ExternalResponsibilitiesStr")
                    if row.get(key)
                ),
                posted_at=row.get("PostedDate"),
                remote=(row.get("WorkplaceType") or "").strip().casefold() == "remote"
                or location.strip().casefold() == "remote",
            ))
        return jobs, total
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        raise SourceError(f"Oracle's job data could not be parsed: {error}") from error


def fetch(source, preferences, client):
    parts = urlsplit(source["url"])
    site = re.search(r"/sites/([A-Za-z0-9_-]+)", parts.path)[1]
    endpoint = f"{parts.scheme}://{parts.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    result = FetchResult()
    for keyword, location in search_queries(preferences):
        if any(char in keyword + location for char in ",;"):
            raise SourceError("Oracle search phrases cannot contain commas or semicolons.")
        seen = set()
        total = 0
        for page in range(preferences["max_pages"]):
            finder = f"findReqs;siteNumber={site},limit={PAGE_SIZE},offset={page * PAGE_SIZE},sortBy=POSTING_DATES_DESC"
            if keyword:
                finder += ",keyword=" + keyword
            if location:
                finder += ",location=" + location
            url = endpoint + "?" + urlencode({"onlyData": "true", "expand": "requisitionList", "finder": finder})
            try:
                payload = client.get(url).json()
            except ValueError as error:
                raise SourceError("Oracle returned invalid JSON rather than job listings.") from error
            jobs, total = parse_jobs(payload, source)
            if not jobs:
                if len(seen) < total:
                    raise SourceError("Oracle returned an empty page before its advertised result count.")
                break
            identifiers = {job.external_id for job in jobs}
            if identifiers <= seen:
                raise SourceError("Oracle repeated a page; pagination may have changed.")
            result.jobs.extend(jobs)
            seen.update(identifiers)
            if len(seen) >= total:
                break
        if len(seen) < total:
            result.warnings.append(
                f"Oracle scanned {len(seen)} of {total} results for '{keyword or 'all roles'}' / "
                f"'{location or 'all locations'}'. Increase the page limit or narrow your search."
            )
    return result

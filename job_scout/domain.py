from dataclasses import dataclass, field
from datetime import UTC, datetime
import re
from urllib.parse import urlsplit, urlunsplit


class SourceError(Exception):
    """An actionable failure while retrieving a career source."""


DEFAULT_PREFERENCES = {
    "keywords": [],
    "locations": [],
    "exclude_keywords": [],
    "remote_only": False,
    "scan_interval_minutes": 0,
    "max_pages": 3,
}
APPLICATION_STATUSES = {"applied", "interviewing", "offer", "rejected"}
STATUSES = {"new", "saved", "dismissed"} | APPLICATION_STATUSES


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def validate_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Enter a career URL of at most 2048 characters.")
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("The career URL is invalid.") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or any(ord(char) <= 32 for char in value)
        or "\\" in value
    ):
        raise ValueError("Use a public HTTP(S) URL without credentials or a custom port.")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def validate_preferences(payload):
    if not isinstance(payload, dict) or set(payload) != set(DEFAULT_PREFERENCES):
        raise ValueError("Supply all preference fields, without additional fields.")
    result = {}
    for key, maximum in (("keywords", 5), ("locations", 3), ("exclude_keywords", 10)):
        values = payload[key]
        if not isinstance(values, list) or len(values) > maximum:
            raise ValueError(f"{key} must be a list of at most {maximum} phrases.")
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 100
            or any(ord(char) < 32 for char in value)
            for value in values
        ):
            raise ValueError(f"{key} must contain nonempty phrases of at most 100 characters.")
        result[key] = list(dict.fromkeys(value.strip() for value in values))
    if type(payload["remote_only"]) is not bool:
        raise ValueError("remote_only must be true or false.")
    result["remote_only"] = payload["remote_only"]
    interval = payload["scan_interval_minutes"]
    if type(interval) is not int or (interval != 0 and not 30 <= interval <= 1440):
        raise ValueError("Scan interval must be 0 (manual) or 30-1440 minutes.")
    pages = payload["max_pages"]
    if type(pages) is not int or not 1 <= pages <= 10:
        raise ValueError("Page limit must be between 1 and 10.")
    result.update(scan_interval_minutes=interval, max_pages=pages)
    return result


def has_phrase(text, phrase):
    return re.search(r"(?<!\w)" + re.escape(phrase.casefold()) + r"(?!\w)", text.casefold()) is not None


def matches_preferences(job, preferences):
    if preferences["remote_only"] and not job["remote"]:
        return False
    if any(has_phrase(job["title"], term) for term in preferences["exclude_keywords"]):
        return False
    searchable = job["title"] + " " + job["description"]
    if preferences["keywords"] and not any(
        has_phrase(searchable, term) for term in preferences["keywords"]
    ):
        return False
    location = " ".join(job["locations"]).casefold()
    return not preferences["locations"] or any(
        term.casefold() in location for term in preferences["locations"]
    )


@dataclass
class Job:
    external_id: str
    title: str
    company: str
    locations: list[str]
    url: str
    description: str = ""
    posted_at: str | None = None
    remote: bool = False


@dataclass
class FetchResult:
    jobs: list[Job] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def search_queries(preferences):
    for keyword in preferences["keywords"] or [""]:
        for location in preferences["locations"] or [""]:
            yield keyword, location

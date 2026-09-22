from urllib.parse import urlsplit
import re

from ..domain import validate_url
from .google import fetch as fetch_google
from .oracle import fetch as fetch_oracle
from .jsonld import fetch as fetch_jsonld


ADAPTERS = {"google": fetch_google, "oracle": fetch_oracle, "jsonld": fetch_jsonld}


def validate_source(payload, current=None):
    if not isinstance(payload, dict) or set(payload) - {"name", "url", "kind", "enabled"}:
        raise ValueError("Source fields are name, url, kind and enabled.")
    if not payload:
        raise ValueError("Supply at least one source field.")
    source = {**(current or {}), **payload}
    name = source.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("Source name must contain 1-100 characters.")
    url = validate_url(source.get("url"))
    parts = urlsplit(url)
    kind = source.get("kind", "auto")
    is_google = parts.hostname in {"www.google.com", "google.com", "careers.google.com"}
    is_oracle = parts.hostname.endswith(".fa.oraclecloud.com")
    if kind == "auto":
        kind = "google" if is_google else "oracle" if is_oracle else "jsonld"
    if not isinstance(kind, str) or kind not in ADAPTERS:
        raise ValueError("Choose auto, google, oracle or jsonld.")
    if kind == "google":
        if not is_google:
            raise ValueError("The Google adapter requires a Google Careers URL.")
        url = "https://www.google.com/about/careers/applications/jobs/results/"
    if kind == "oracle" and (
        not is_oracle or not re.search(r"/sites/[A-Za-z0-9_-]+(?:/|$)", parts.path)
    ):
        raise ValueError("Oracle URLs must use a *.fa.oraclecloud.com portal with /sites/SITE_ID.")
    enabled = source.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("enabled must be true or false.")
    return {"name": name.strip(), "url": url, "kind": kind, "enabled": enabled}

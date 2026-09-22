import ipaddress
import logging
import socket
import time
from urllib.parse import urljoin, urlsplit

from protego import Protego
import requests

from .domain import SourceError, validate_url


USER_AGENT = "JobScout/0.1 (local personal career search)"
LOGGER = logging.getLogger(__name__)


def public_url(url):
    try:
        url = validate_url(url)
        host = urlsplit(url).hostname
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not addresses or any(
            not ipaddress.ip_address(address[4][0]).is_global for address in addresses
        ):
            raise ValueError("Private, loopback and reserved network addresses are not allowed.")
    except (ValueError, OSError) as error:
        raise SourceError(f"Cannot access URL: {error}") from error
    return url


class Fetcher:
    """Rate-limited public HTTP access, with robots rules and bounded responses."""

    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
        self.rules = {}
        self.last_request = {}
        self.warnings = []

    def close(self):
        self.session.close()

    def _robots(self, origin):
        if origin not in self.rules:
            response = self._download(origin + "/robots.txt", check_robots=False, max_bytes=1_000_000)
            if response.status_code == 429 or response.status_code >= 500:
                raise SourceError(f"robots.txt returned HTTP {response.status_code}; scan deferred.")
            if 400 <= response.status_code < 500:
                # RFC 9309 2.3.1.3: unavailable robots files impose no crawl rules.
                if response.status_code not in {404, 410}:
                    warning = (
                        f"robots.txt unavailable (HTTP {response.status_code}); "
                        "no crawl rules could be read. Check this site's terms before use."
                    )
                    self.warnings.append(warning)
                    LOGGER.warning("%s: %s", origin, warning)
                self.rules[origin] = Protego.parse("")
            else:
                if "<html" in response.text[:1000].lower():
                    raise SourceError("robots.txt returned an HTML page instead of crawl rules.")
                self.rules[origin] = Protego.parse(response.text)
        return self.rules[origin]

    def _download(self, url, *, check_robots=True, max_bytes=8_000_000):
        for _ in range(6):
            url = public_url(url)
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}"
            delay = 1.0
            if check_robots:
                rules = self._robots(origin)
                if not rules.can_fetch(url, USER_AGENT):
                    raise SourceError("This URL is disallowed by the site's robots.txt.")
                delay = max(delay, rules.crawl_delay(USER_AGENT) or 0)
                if delay > 60:
                    raise SourceError("Site requests a crawl delay above 60 seconds; scan deferred.")
            wait = delay - (time.monotonic() - self.last_request.get(origin, 0))
            if wait > 0:
                time.sleep(wait)
            self.last_request[origin] = time.monotonic()
            try:
                with self.session.get(url, timeout=(10, 25), allow_redirects=False, stream=True) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = response.headers.get("Location")
                        if not target:
                            raise SourceError("The source returned a redirect without a destination.")
                        url = urljoin(url, target)
                        continue
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise SourceError("Source response exceeds the download size limit.")
                        chunks.append(chunk)
                    response._content = b"".join(chunks)
                    response._content_consumed = True
                    if not response.encoding or response.encoding.lower() == "iso-8859-1":
                        response.encoding = "utf-8"
                    return response
            except requests.RequestException as error:
                raise SourceError(f"Network request failed: {error}") from error
        raise SourceError("The source redirected too many times.")

    def get(self, url):
        response = self._download(url)
        if response.status_code >= 400:
            raise SourceError(
                f"Source returned HTTP {response.status_code}. "
                "Access restrictions are not bypassed; try again later or open the portal."
            )
        return response

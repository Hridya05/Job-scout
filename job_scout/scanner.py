from datetime import UTC, datetime, timedelta
import logging
import threading

from .adapters import ADAPTERS
from .domain import SourceError, validate_url
from .http import Fetcher


LOGGER = logging.getLogger(__name__)


class Scanner:
    def __init__(self, store, client_factory=Fetcher):
        self.store = store
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.running = False
        self.last_error = None
        self.stop_event = threading.Event()
        self.scheduler = None

    def next_run(self):
        interval = self.store.preferences()["scan_interval_minutes"]
        last = self.store.last_scan()
        if not interval or self.running or not any(source["enabled"] for source in self.store.sources()):
            return None
        if last is None:
            return datetime.now(UTC)
        return datetime.fromisoformat(last["finished_at"] or last["started_at"]) + timedelta(minutes=interval)

    def state(self):
        with self.lock:
            next_run = self.next_run()
            return {
                "running": self.running, "last_run": self.store.last_scan(),
                "next_run_at": next_run.isoformat() if next_run else None,
                "last_error": self.last_error,
            }

    def start(self):
        with self.lock:
            if self.running:
                raise ValueError("A scan is already running.")
            sources = [source for source in self.store.sources() if source["enabled"]]
            if not sources:
                raise ValueError("Enable at least one source before scanning.")
            preferences = self.store.preferences()
            identifier = self.store.start_scan()
            self.running = True
            self.last_error = None
            thread = threading.Thread(
                target=self._run, args=(identifier, sources, preferences), daemon=True, name="job-scan"
            )
            try:
                thread.start()
            except RuntimeError:
                self.running = False
                self.store.finish_scan(identifier, "Unable to start scan worker.")
                raise

    def _run(self, identifier, sources, preferences):
        errors = []
        try:
            for source in sources:
                client = self.client_factory()
                try:
                    result = ADAPTERS[source["kind"]](source, preferences, client)
                    for job in result.jobs:
                        job.url = validate_url(job.url)
                        if not job.external_id or not job.title.strip():
                            raise SourceError("A listing is missing its identifier or title.")
                    self.store.save_jobs(source, result.jobs, result.warnings + client.warnings)
                except (SourceError, ValueError) as error:
                    message = str(error)
                    LOGGER.warning("Scan failed for %s: %s", source["name"], message)
                    self.store.source_error(source["id"], message)
                    errors.append(f"{source['name']}: {message}")
                except Exception:
                    # A plugin failure must not kill the worker or hide a source failure.
                    LOGGER.exception("Unexpected adapter failure for %s", source["name"])
                    message = "Unexpected adapter failure. See the app console for details."
                    self.store.source_error(source["id"], message)
                    errors.append(f"{source['name']}: {message}")
                finally:
                    client.close()
        except Exception:
            LOGGER.exception("Scan worker failed")
            errors.append("The scan worker failed. See the app console for details.")
        finally:
            with self.lock:
                self.last_error = "\n".join(errors) or None
                try:
                    self.store.finish_scan(identifier, self.last_error)
                except Exception:
                    LOGGER.exception("Could not persist scan completion")
                    self.last_error = "Could not save scan completion. Check the app console and disk space."
                self.running = False

    def start_scheduler(self):
        if self.scheduler:
            return
        self.scheduler = threading.Thread(target=self._schedule, daemon=True, name="job-scheduler")
        self.scheduler.start()

    def _schedule(self):
        while not self.stop_event.wait(15):
            try:
                with self.lock:
                    due = self.next_run()
                    if due and due <= datetime.now(UTC):
                        self.start()
            except Exception:
                LOGGER.exception("Scheduled scan failed")
                self.last_error = "Scheduled scan failed. See the app console for details."

    def stop(self):
        self.stop_event.set()

# Job Scout

A local-first career search dashboard and application tracker. Your preferences,
sources, saved jobs, application stages and notes live in a SQLite database on
your computer. No account, API key, paid service or cloud database is needed.

## Start on Windows

Requires Python 3.11 or newer.

Double-click **Start Job Scout.cmd** in this directory. On first launch it creates
an isolated `.venv` and installs the dependencies. It opens
**http://127.0.0.1:8765** in your browser. Keep the terminal window open while
using the application; Ctrl+C stops it. The app uses a local Waitress server,
not Flask's development server, and is not exposed to your network.

Alternatively, from PowerShell:

```powershell
Set-Location 'C:\Users\PC\Projects\job-scout'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m job_scout --open-browser
```

Use `--port 8766` if the default port is occupied. Use `--data-dir 'D:\JobScoutData'`
to choose a different database directory. Run only one app instance against a
given data directory. Do not expose this single-user application through a
reverse proxy or public tunnel.

## Use it

1. Set keyword phrases, locations, excluded title phrases and an optional
   remote-only filter. Blank filters include everything. Click Save before scanning.
2. Google and JPMorgan Chase are preconfigured. Add, edit, disable or remove
   sources in the dashboard. Disable a source to retain its job/application
   history; **deleting a source also deletes its jobs and application notes**.
3. Click Scan. Results are stored and deduplicated; repeat scans update listings
   without overwriting your application status or notes. Source errors and
   coverage limits are shown separately from genuine empty results.
4. Search the collected jobs, save interesting ones, and mark applications as
   Applied, Interviewing, Offer or Rejected. The Applications view keeps these
   visible even if you later change your search preferences. Notes can hold
   interview details, follow-up reminders or your application reference.
5. Export the current view to CSV for a spreadsheet. CSV exports are not full
   backups and cannot be imported into the app.

Statuses and dates are **manually tracked**: marking Applied records when you
first mark the job as part of your application pipeline, not a verified
submission timestamp. The app never applies on your behalf and does not access
your email or employer accounts. Dismissed jobs disappear from Matches but
remain in All jobs.

### How matching and scheduling work

- Any keyword phrase may match the title or available description. Any requested
  location may match a listed location; keyword and location groups must both
  pass. Matching is case-insensitive. Exclusions apply only to job titles.
- Use the portal's spelling for locations (for example, `Bengaluru` rather than
  `Bangalore`). There is no geocoding, skills inference or experience-level
  inference. Add specific title exclusions or keyword phrases as needed.
- Remote-only requires explicit remote metadata, not merely the word "remote"
  somewhere in a description. A remote job can still have country restrictions.
  Missing metadata may exclude otherwise suitable roles.
- Filters immediately re-evaluate stored jobs. Run another scan after changing
  preferences to fetch new candidates. Keywords and locations are also sent to
  Google/Oracle search APIs to improve coverage, one search per combination.
- Scheduled scans are off by default. Set an interval of 30-1440 minutes to
  enable them. With no previous scan, the first scheduled scan starts within
  about 15 seconds. Later intervals count from the preceding scan's completion.
  Only one scan runs at once. **The app and computer must remain running**;
  this is not a Windows service and cannot wake a sleeping computer.
- Each Oracle query scans up to the selected number of pages, 25 jobs per page.
  Limits are per keyword/location combination, not a total across all sources.
  Up to five keywords and three locations are supported.
- Stored listings are observations, not guaranteed current openings. The app
  shows first/last seen dates and never infers that an opening is closed merely
  because a partial scan did not return it. Confirm availability on the official
  posting before applying.

## Supported sources and important limits

| Adapter | What it supports | Limits |
| --- | --- | --- |
| Google | Public Google Careers search data embedded in the result page | First page only (currently up to 20 jobs per query). Google's robots rules restrict pagination. Narrow keywords/locations; this is not an exhaustive Google search. |
| Oracle Recruiting | Public `*.fa.oraclecloud.com` candidate portals, including JPMorgan Chase | Public requisition search with keyword/location filters and capped pagination. Available descriptions may be summaries, and Oracle phrase filters cannot contain comma/semicolon delimiters. |
| JSON-LD | Pages containing structured Schema.org `JobPosting` objects, including arrays, graphs and nested item lists | Only embedded postings and same-site `rel=next` pages are scanned. Does not discover every job-detail link or execute JavaScript. An unsupported page produces a visible error, not a fake empty success. |

Auto detection picks Google or Oracle for their known hosts, otherwise JSON-LD.
An arbitrary career-site URL is **not** a guarantee that scraping is supported.
Other JavaScript-heavy portals, Workday sites, login walls and changed page
formats need dedicated adapters.

Use sources only where their terms permit your use. Requests identify themselves
as JobScout, are spaced at least one second apart per origin, obey readable
`robots.txt` rules (including wildcards and crawl delays), and are limited in
size/time. Redirect destinations are checked and private/reserved IP addresses
are rejected. No proxy rotation, CAPTCHA solving or access-control bypass exists.
HTTP errors on listing pages fail the scan.

Robots handling follows RFC 9309: a missing/unavailable robots file (HTTP 4xx,
other than 429) has no readable crawl rules. Non-404/410 cases are explicitly
shown as warnings; JPMC currently returns 403 for its robots file while its
public requisition API returns listings. A robots warning is **not permission
from the site**. Review its terms before enabling regular scans. Network
failures, rate limits, HTML challenges and server errors while reading robots
defer the scan.

## Data, backups and privacy

The default database is `data\job-scout.sqlite3`. Stop the app before copying the
entire `data` directory for a backup. Restore it while the app is stopped.
The `.venv` and `data` directories are excluded from Git.

Data is local but **not encrypted at rest**. Anyone with access to your Windows
account or database can read application notes. Do not put passwords or identity
documents in notes. Preferences are sent only to the configured career sites as
search queries. There is no analytics or external frontend CDN.

## Modify or extend

| File | Responsibility |
| --- | --- |
| `job_scout\domain.py` | Job model, preference validation and matching |
| `job_scout\adapters\` | Portal-specific parsing/search; shared adapter registry |
| `job_scout\http.py` | Network rules, redirects, crawl policies and rate limits |
| `job_scout\storage.py` | SQLite persistence, deduplication and tracking |
| `job_scout\scanner.py` | Background scans and optional scheduling |
| `job_scout\app.py` | Local JSON API, validation and CSV export |
| `job_scout\templates\index.html` | Dashboard structure |
| `job_scout\static\app.js` | Browser interactions |
| `job_scout\static\style.css` | Appearance and responsive layout |

To add a portal, create an adapter exposing `fetch(source, preferences, client)`.
Return `FetchResult(jobs=[Job(...)], warnings=[...])`; use a stable external job
ID and an official posting URL. Make network requests through `client.get` so
crawl policies apply. Raise `SourceError` for blocked access, malformed responses
or unsupported markup; do not silently convert failures to an empty job list.
Register the adapter and its URL validation in `adapters\__init__.py`, add its
option to the source form, and add offline tests. A changed source URL/adapter
gets a new deduplication namespace, preserving its previous tracked jobs.

Preference changes should update the defaults and validator in `domain.py`,
the dashboard form/serialization, and tests together. Future database schema
changes should use explicit migrations before modifying an existing database.

Run the offline regression suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For dependency updates, edit `requirements.txt` and install it again in `.venv`.
After code changes, stop and restart the application; automatic reload is not
enabled.

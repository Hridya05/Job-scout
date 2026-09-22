import argparse
import logging
from pathlib import Path
import threading
import webbrowser

from waitress import create_server

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description="Local career search and application tracker.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(args.data_dir / "job-scout.sqlite3")
    try:
        server = create_server(app, host="127.0.0.1", port=args.port, threads=4)
    except OSError as error:
        parser.exit(1, f"Could not start Job Scout: {error}\nChoose a different --port if it is already in use.\n")
    scanner = app.extensions["scanner"]
    scanner.start_scheduler()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Job Scout: {url}\nData: {args.data_dir.resolve()}\nPress Ctrl+C to stop.", flush=True)
    if args.open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.run()
    except KeyboardInterrupt:
        print("\nStopping Job Scout.", flush=True)
    finally:
        scanner.stop()
        server.close()


if __name__ == "__main__":
    main()

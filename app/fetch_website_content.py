import os
import time
import signal
import ipaddress
import requests
from datetime import datetime
from urllib.parse import urlparse
from typing import Dict, List
import threading
import queue

from dotenv import load_dotenv
from pymongo import MongoClient
from scrapling.fetchers import StealthySession
import tldextract


load_dotenv()
RUNNING = True
RDAP_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=None)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://host.docker.internal:27017/")

def handle_signal(sig, frame):
    global RUNNING
    print("\nReceived signal, shutting down gracefully...")
    RUNNING = False

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def fetch_rdap_data(url: str) -> Dict:
    parsed = urlparse(url if "://" in url else f"//{url}")
    hostname = parsed.hostname
    if not hostname:
        return {"rdap_status": "error", "error": "Invalid URL hostname"}

    try:
        normalized_host = hostname.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as e:
        return {"rdap_status": "error", "error": f"Invalid IDN hostname: {e}"}

    lookup_path = "domain"
    lookup_value = normalized_host

    try:
        lookup_value = str(ipaddress.ip_address(normalized_host))
        lookup_path = "ip"
    except ValueError:
        extracted = RDAP_EXTRACTOR(normalized_host)
        if not extracted.suffix or not extracted.domain:
            return {
                "rdap_status": "error",
                "error": f"Unable to determine registrable domain for host '{normalized_host}'",
            }
        lookup_value = (
            getattr(extracted, "top_domain_under_public_suffix", None)
            or extracted.registered_domain
        )

    try:
        response = requests.get(
            f"https://rdap.org/{lookup_path}/{lookup_value}",
            timeout=10,
            headers={"Accept": "application/rdap+json, application/json"},
        )
        if response.status_code == 200:
            data = response.json()
            data["_rdap_lookup"] = {
                "input_url": url,
                "hostname": normalized_host,
                "lookup_path": lookup_path,
                "lookup_value": lookup_value,
            }
            return data
        return {"rdap_status": "error", "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"rdap_status": "error", "error": str(e)}


class MongoManager:
    def __init__(self):
        self.client = MongoClient(MONGO_URI)
        self.db = self.client.phishing_db
        self.urls = self.db.phishing_urls
        self.content = self.db.website_content

    def ensure_visited_field(self):
        self.urls.update_many({"visited": {"$exists": False}}, {"$set": {"visited": False}})

    def get_unvisited(self) -> List[str]:
        urls = self.urls.find(
            {"$or": [{"visited": False}, {"visited": {"$exists": False}}]}, {"url": 1, "_id": 0}
        )
        return [u["url"] for u in urls]

    def mark_visited(self, url: str):
        self.urls.update_one(
            {"url": url}, {"$set": {"visited": True, "visited_at": datetime.utcnow()}}
        )

    def mark_error(self, url: str, error: str):
        self.urls.update_one(
            {"url": url}, {"$set": {"visited": True, "visited_at": datetime.utcnow(), "error": error}}
        )

    def save_content(self, data: Dict):
        self.content.insert_one(data)

    def close(self):
        self.client.close()


class Browser:
    def __init__(self):
        try:
            self.session = StealthySession(
                headless=True,
                network_idle=True,
                load_dom=True,
                humanize=True,
                solve_cloudflare=True,
                timeout=60_000,
                wait=5_000,
            )
            start = getattr(self.session, "start", None)
            if callable(start):
                start()
        except Exception as e:
            print(f"Failed to initialize Scrapling Stealth session: {e}")
            self.session = None

    def close(self):
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass

    @staticmethod
    def _decode_body(response) -> str:
        body = getattr(response, "body", b"") or b""
        if isinstance(body, str):
            return body
        encoding = getattr(response, "encoding", None) or "utf-8"
        return body.decode(encoding, errors="replace")

    @staticmethod
    def _extract_title(response) -> str:
        titles = response.css("title::text").getall()
        if not titles:
            return ""
        return titles[0].strip()

    @staticmethod
    def _as_dict(value) -> Dict:
        try:
            return dict(value)
        except Exception:
            return {}

    def _build_metadata(self, url: str, response, elapsed_ms: float) -> Dict:
        redirect_history = []
        for redirect in getattr(response, "history", []) or []:
            redirect_history.append(
                {
                    "status_code": getattr(redirect, "status", None),
                    "url": getattr(redirect, "url", None),
                    "headers": self._as_dict(getattr(redirect, "headers", {})),
                    "timestamp": datetime.utcnow(),
                }
            )

        final_url = getattr(response, "url", None) or url
        body = getattr(response, "body", b"") or b""
        return {
            "url": url,
            "status_code": getattr(response, "status", None),
            "headers": self._as_dict(getattr(response, "headers", {})),
            "encoding": getattr(response, "encoding", None),
            "elapsed_ms": elapsed_ms,
            "final_url": final_url,
            "redirect_count": len(redirect_history),
            "redirect_history": redirect_history,
            "content_length": len(body),
            "timestamp": datetime.utcnow(),
        }

    def fetch(self, url: str) -> Dict:
        if not self.session:
            error = "No Scrapling Stealth session initialized"
            return {
                "url": url,
                "status": "error",
                "error": error,
                "metadata": {"url": url, "error": error, "timestamp": datetime.utcnow()},
            }

        started_at = time.perf_counter()
        try:
            response = self.session.fetch(
                url,
                wait_selector="body",
            )
        except Exception as e:
            return {
                "url": url,
                "status": "error",
                "error": str(e),
                "metadata": {"url": url, "error": str(e), "timestamp": datetime.utcnow()},
            }

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        html = self._decode_body(response)
        title = self._extract_title(response)
        metadata = self._build_metadata(url, response, elapsed_ms)

        return {
            "url": url,
            "title": title,
            "html": html,
            "fetched_at": datetime.utcnow(),
            "metadata": metadata,
            "error": None,
        }


def worker_thread(thread_id: int, q: queue.Queue, mongo: MongoManager):
    browser = Browser()
    print(f"Worker-{thread_id} started.")
    try:
        while RUNNING:
            try:
                url = q.get_nowait()
            except queue.Empty:
                break

            print(f"Worker-{thread_id} fetching: {url}")
            result = browser.fetch(url)
            rdap_info = fetch_rdap_data(url)
            result.update({"rdap": rdap_info})

            try:
                mongo.save_content(result)
                mongo.mark_visited(url)
                if result.get("error"):
                    mongo.mark_error(url, result["error"])
            except Exception as e:
                print(f"Worker-{thread_id} Mongo error for {url}: {e}")

            q.task_done()
            if not RUNNING:
                break
    finally:
        browser.close()
        print(f"Worker-{thread_id} stopped.")


def process_sites_parallel(mongo: MongoManager, workers: int = 4):
    urls = mongo.get_unvisited()
    if not urls:
        print("No unvisited URLs found.")
        return

    q: queue.Queue = queue.Queue()
    for u in urls:
        q.put(u)

    threads = []
    for i in range(workers):
        t = threading.Thread(target=worker_thread, args=(i + 1, q, mongo), daemon=True)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            if not RUNNING:
                break
            time.sleep(1)
    finally:
        for t in threads:
            t.join(timeout=5)


def run(interval_min: int = 10, workers: int = 4):
    print(f"Starting parallel fetcher with metadata extraction and {workers} workers (every {interval_min} min).")
    mongo = MongoManager()
    mongo.ensure_visited_field()

    try:
        while RUNNING:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[{timestamp}] Checking for unvisited websites...")
            process_sites_parallel(mongo, workers=workers)
            print(f"[{timestamp}] Cycle complete. Waiting {interval_min} minutes...")
            for _ in range(interval_min * 60):
                if not RUNNING:
                    break
                time.sleep(1)
    finally:
        mongo.close()
        print("Shutdown complete.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Parallel website fetcher with RDAP, metadata, and Scrapling Stealth")
    parser.add_argument("--period", type=int, default=10, help="Interval in minutes")
    parser.add_argument("--workers", type=int, default=int(os.getenv("PARALLEL_WORKERS", "3")), help="Number of parallel browser workers")
    args = parser.parse_args()

    if args.period <= 0:
        raise ValueError("Period must be positive")

    time.sleep(5)
    run(args.period, args.workers)

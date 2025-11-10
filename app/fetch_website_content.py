import os
import sys
import time
import signal
import hashlib
import requests
from datetime import datetime
from urllib.parse import urlparse, quote_plus
from typing import Dict, List
import threading
import queue

from dotenv import load_dotenv
from pymongo import MongoClient
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import urllib3

from filelock import FileLock
from pathlib import Path
import tempfile


load_dotenv()
RUNNING = True

browser_init_lock = FileLock(Path(tempfile.gettempdir()) / "chromedriver_init.lock")

def handle_signal(sig, frame):
    global RUNNING
    print("\nReceived signal, shutting down gracefully...")
    RUNNING = False

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def fetch_rdap_data(url: str) -> Dict:
    domain = urlparse(url).netloc.lstrip("www.")
    if not domain:
        return {"rdap_status": "error", "error": "Invalid domain"}

    try:
        response = requests.get(f"https://rdap.org/domain/{domain}", timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data
        return {"rdap_status": "error", "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"rdap_status": "error", "error": str(e)}


def extract_request_metadata(url: str) -> Dict:
    try:
        response = requests.get(url, timeout=10, allow_redirects=True)
        redirect_history = []
        for redirect in response.history:
            redirect_history.append({
                "status_code": redirect.status_code,
                "url": redirect.url,
                "headers": dict(redirect.headers),
                "timestamp": datetime.utcnow()
            })

        metadata = {
            "url": url,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "encoding": response.encoding,
            "elapsed_ms": response.elapsed.total_seconds() * 1000,
            "final_url": response.url,
            "redirect_count": len(response.history),
            "redirect_history": redirect_history,
            "content_length": len(response.content),
            "timestamp": datetime.utcnow()
        }
        return metadata
    except Exception as e:
        return {"url": url, "error": str(e)}


class MongoManager:
    def __init__(self):
        host = os.getenv("MONGO_HOST", "mongodb")
        port = int(os.getenv("MONGO_PORT", "27017"))
        # url encode username and password
        mongo_uri = f"mongodb://{quote_plus(os.getenv('MONGO_USER'))}:{quote_plus(os.getenv('MONGO_PASSWORD'))}@{host}:{port}/phishing_db?authSource=admin"
        self.client = MongoClient(mongo_uri)
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
            options = uc.ChromeOptions()
            options.headless = True
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-blink-features=AutomationControlled")
            
            with browser_init_lock:
                self.driver = uc.Chrome(options=options, user_multi_procs=True)
                self.driver.set_page_load_timeout(60)
                self.driver.set_script_timeout(30)
        except Exception as e:
            print(f"Failed to initialize undetected_chromedriver: {e}")
            self.driver = None

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

    @staticmethod
    def _safe_filename(url: str) -> str:
        domain = urlparse(url).netloc.replace("www.", "")
        safe_domain = "".join(c for c in domain if c.isalnum() or c in ".-_")[:50]
        hash_part = hashlib.md5(url.encode()).hexdigest()[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{safe_domain}_{hash_part}_{timestamp}.png"

    def fetch(self, url: str) -> Dict:
        if not self.driver:
            return {"url": url, "status": "error", "error": "No driver initialized"}

        try:
            self.driver.get(url)
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except (TimeoutException, WebDriverException, urllib3.exceptions.ReadTimeoutError) as e:
            return {"url": url, "status": "error", "error": str(e)}

        time.sleep(5)
        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        title = soup.title.string if soup.title else ""

        screenshot_dir = "data/screenshots"
        os.makedirs(screenshot_dir, exist_ok=True)
        filename = self._safe_filename(url)
        path = os.path.join(screenshot_dir, filename)
        try:
            self.driver.save_screenshot(path)
        except Exception:
            path = None

        return {
            "url": url,
            "title": title,
            "html": self.driver.page_source,
            "screenshot_path": path,
            "fetched_at": datetime.utcnow(),
            "error": None
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
            metadata = extract_request_metadata(url)
            result.update({"rdap": rdap_info, "metadata": metadata})

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

    parser = argparse.ArgumentParser(description="Parallel Website Fetcher with RDAP, Metadata, and undetected_chromedriver")
    parser.add_argument("--period", type=int, default=10, help="Interval in minutes")
    parser.add_argument("--workers", type=int, default=int(os.getenv("PARALLEL_WORKERS", "3")), help="Number of parallel browser workers")
    args = parser.parse_args()

    if args.period <= 0:
        raise ValueError("Period must be positive")

    time.sleep(5)
    run(args.period, args.workers)

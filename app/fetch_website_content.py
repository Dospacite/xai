import os
import sys
import time
import signal
import hashlib
import requests
from datetime import datetime
from urllib.parse import urlparse
from typing import Dict, List

from dotenv import load_dotenv
from pymongo import MongoClient
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException


load_dotenv()
RUNNING = True

def handle_signal(sig, frame):
    global RUNNING
    print("\nReceived signal, shutting down gracefully...")
    RUNNING = False
    sys.exit(0)

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
            return {
                "rdap_status": "success",
                "domain_name": data.get("ldhName"),
                "handle": data.get("handle"),
                "registrar": data.get("registrar", {}).get("name") if isinstance(data.get("registrar"), dict) else data.get("registrar"),
                "country": data.get("country"),
            }
        return {"rdap_status": "error", "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"rdap_status": "error", "error": str(e)}


class MongoManager:
    def __init__(self):
        host = os.getenv("MONGO_HOST", "mongodb")
        port = int(os.getenv("MONGO_PORT", "27017"))
        self.client = MongoClient(f"mongodb://{host}:{port}/")
        self.db = self.client.phishing_db
        self.feed = self.db.phishing_feed
        self.content = self.db.website_content

    def ensure_visited_field(self):
        self.feed.update_many({"visited": {"$exists": False}}, {"$set": {"visited": False}})

    def get_unvisited(self) -> List[str]:
        urls = self.feed.find(
            {"$or": [{"visited": False}, {"visited": {"$exists": False}}]}, {"url": 1, "_id": 0}
        )
        return [u["url"] for u in urls]

    def mark_visited(self, url: str):
        self.feed.update_one(
            {"url": url}, {"$set": {"visited": True, "visited_at": datetime.utcnow()}}
        )

    def save_content(self, data: Dict):
        simplified = {
            "url": data.get("url"),
            "title": data.get("title"),
            "screenshot_path": data.get("screenshot_path"),
            "status": data.get("status"),
            "fetched_at": data.get("fetched_at"),
            "error": data.get("error"),
            "rdap": data.get("rdap", {}),
        }
        self.content.insert_one(simplified)

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
            self.driver = uc.Chrome(options=options)
        except Exception as e:
            print(f"Failed to initialize undetected_chromedriver: {e}")
            self.driver = None

    def close(self):
        if self.driver:
            self.driver.quit()

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

        print(f"Visiting: {url}")
        try:
            self.driver.get(url)
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except (TimeoutException, WebDriverException) as e:
            return {"url": url, "status": "error", "error": str(e)}

        time.sleep(3)
        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        title = soup.title.string if soup.title else ""

        screenshot_dir = "data/screenshots"
        os.makedirs(screenshot_dir, exist_ok=True)
        filename = self._safe_filename(url)
        path = os.path.join(screenshot_dir, filename)
        self.driver.save_screenshot(path)

        return {
            "url": url,
            "title": title,
            "screenshot_path": path,
            "fetched_at": datetime.utcnow(),
            "status": "success",
            "error": None
        }


def process_sites(mongo: MongoManager, browser: Browser):
    urls = mongo.get_unvisited()
    if not urls:
        print("No unvisited URLs found.")
        return

    for i, url in enumerate(urls, 1):
        if not RUNNING:
            break
        print(f"\n[{i}/{len(urls)}] {url}")

        result = browser.fetch(url)
        rdap_info = fetch_rdap_data(url)
        result.update({"rdap": rdap_info})

        mongo.save_content(result)
        mongo.mark_visited(url)

def run(interval_min: int = 10):
    print(f"Starting periodic fetcher with undetected_chromedriver (every {interval_min} min).")
    mongo = MongoManager()
    mongo.ensure_visited_field()
    browser = Browser()

    try:
        while RUNNING:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[{timestamp}] Checking for unvisited websites...")
            process_sites(mongo, browser)
            print(f"[{timestamp}] Cycle complete. Waiting {interval_min} minutes...")
            for _ in range(interval_min * 60):
                if not RUNNING:
                    break
                time.sleep(1)
    finally:
        browser.close()
        mongo.close()
        print("Shutdown complete.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Website Fetcher with RDAP + undetected_chromedriver")
    parser.add_argument("--period", type=int, default=10, help="Interval in minutes")
    args = parser.parse_args()

    if args.period <= 0:
        raise ValueError("Period must be positive")

    time.sleep(5)
    run(args.period)

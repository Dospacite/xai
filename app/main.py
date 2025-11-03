import os
import time
import csv
import requests
from io import StringIO
from datetime import datetime
from pymongo import MongoClient
from dotenv import load_dotenv
import argparse


load_dotenv()
GITHUB_PAT = os.getenv("GITHUB_PAT")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongodb:27017/")
UPDATE_PERIOD = int(os.getenv("UPDATE_PERIOD", "10"))  # minutes

FEED_URL = "https://raw.githubusercontent.com/openphish/academic/main/feed.csv"


def get_db():
    client = MongoClient(MONGO_URI)
    return client.phishing_db


def fetch_feed(github_pat: str):
    """Fetch phishing feed CSV and return parsed entries."""
    headers = {"Authorization": f"token {github_pat}", "Accept": "text/csv"}
    response = requests.get(FEED_URL, headers=headers, timeout=15)
    response.raise_for_status()

    reader = csv.DictReader(StringIO(response.text))
    return [row for row in reader if any(row.values())]


def update_database(feed_data):
    """Insert only new URLs into MongoDB."""
    db = get_db()
    collection = db.phishing_urls
    collection.create_index("url", unique=True)

    existing_urls = {doc["url"] for doc in collection.find({}, {"url": 1})}
    new_entries = [
        {**entry, "added_at": datetime.utcnow()}
        for entry in feed_data
        if entry.get("url") not in existing_urls
    ]

    if new_entries:
        result = collection.insert_many(new_entries)
        print(f"Added {len(result.inserted_ids)} new URLs.")
    else:
        print("No new entries found.")


def run_once():
    """Single update cycle."""
    if not GITHUB_PAT:
        raise ValueError("GITHUB_PAT is not set in environment variables.")

    try:
        feed = fetch_feed(GITHUB_PAT)
        print(f"Fetched {len(feed)} entries from feed.")
        update_database(feed)
    except Exception as e:
        print(f"Error during update: {e}")


def run_periodically(period_minutes: int):
    print(f"Running phishing feed updater every {period_minutes} minutes (Ctrl+C to stop).")

    while True:
        print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] Starting update...")
        run_once()
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Update complete.")
        time.sleep(period_minutes * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phishing feed updater")
    parser.add_argument("--period", type=int, default=UPDATE_PERIOD, help="Update period in minutes")
    args = parser.parse_args()

    run_periodically(args.period)

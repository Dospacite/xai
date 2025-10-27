import requests
import csv
import os
import argparse
import time
import signal
import sys
from typing import List, Dict
from io import StringIO
from dotenv import load_dotenv
from pymongo import MongoClient
from datetime import datetime

# Load environment variables from .env file
load_dotenv()


def fetch_phishing_feed(github_pat: str) -> List[Dict[str, str]]:
    url = "https://raw.githubusercontent.com/openphish/academic/main/feed.csv"
    
    # GitHub requires the token to be passed in the Authorization header
    headers = {
        "Authorization": f"token {github_pat}",
        "Accept": "text/csv"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        csv_data = response.text
        reader = csv.DictReader(StringIO(csv_data))
        
        # Filter out empty rows (where all values are empty strings)
        data = [row for row in reader if any(row.values())]
        
        return data
    
    except requests.exceptions.RequestException as e:
        print(f"Error fetching feed: {e}")
        raise


def save_feed_to_file(github_pat: str, output_file: str = "feed.csv") -> None:
    data = fetch_phishing_feed(github_pat)
    
    # Write to file
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        if data:
            fieldnames = data[0].keys()
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator='\n')
            writer.writeheader()
            writer.writerows(data)
    
    print(f"Successfully saved {len(data)} records to {output_file}")


def get_mongo_client():
    """Connect to MongoDB using Docker hostname."""
    mongo_host = os.getenv("MONGO_HOST", "mongodb")  # Use "mongodb" for Docker, "localhost" for local
    mongo_port = int(os.getenv("MONGO_PORT", "27017"))
    mongo_uri = f"mongodb://{mongo_host}:{mongo_port}/"
    
    client = MongoClient(mongo_uri)
    return client


def get_existing_urls(collection, url_field: str = "url"):
    """Get all existing URLs from the database."""
    existing_urls = set()
    for doc in collection.find({}, {url_field: 1}):
        if url_field in doc:
            existing_urls.add(doc[url_field])
    return existing_urls


def add_new_entries_to_db(github_pat: str) -> None:
    """Fetch feed and add only new entries to MongoDB."""
    data = fetch_phishing_feed(github_pat)
    
    if not data:
        print("No data fetched from feed.")
        return
    
    # Connect to MongoDB with retry logic
    try:
        client = get_mongo_client()
        # Test the connection
        client.admin.command('ping')
    except Exception as e:
        print(f"Failed to connect to MongoDB: {e}")
        raise
    
    try:
        db = client.phishing_db
        collection = db.phishing_feed
        
        # Create index on 'url' field if it doesn't exist (for faster lookups)
        try:
            collection.create_index("url", unique=True)
        except Exception:
            pass  # Index might already exist
        
        # Get existing URLs
        existing_urls = get_existing_urls(collection)
        print(f"Found {len(existing_urls)} existing entries in database")
        
        # Filter out entries that already exist
        new_entries = []
        for entry in data:
            if entry.get("url") not in existing_urls:
                # Add timestamp when entry was added to our database
                entry["added_at"] = datetime.utcnow()
                new_entries.append(entry)
        
        if new_entries:
            # Insert new entries
            result = collection.insert_many(new_entries)
            print(f"Successfully added {len(result.inserted_ids)} new entries to database")
        else:
            print("No new entries to add. Database is up to date.")
    
    finally:
        client.close()


running = True


def signal_handler(sig, frame):
    global running
    print("\nShutting down gracefully...")
    running = False
    sys.exit(0)


def run_periodically(token: str, period_minutes: int):
    global running
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    period_seconds = period_minutes * 60
    
    print(f"Starting phishing feed agent (period: {period_minutes} minutes)")
    print(f"Press Ctrl+C to stop")
    
    iteration = 0
    
    while running:
        iteration += 1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            print(f"\n[{timestamp}] Iteration {iteration}: Fetching feed and updating database...")
            add_new_entries_to_db(token)
            print(f"[{timestamp}] Iteration {iteration} completed successfully")
            
        except Exception as e:
            print(f"[{timestamp}] Iteration {iteration} failed: {e}")
        
        if running:
            print(f"Waiting {period_minutes} minutes until next iteration...")
            # Sleep in small increments to allow for graceful shutdown
            for _ in range(period_seconds):
                if not running:
                    break
                time.sleep(1)


if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Phishing feed fetcher and database updater agent"
    )
    parser.add_argument(
        "--period",
        type=int,
        default=10,
        help="Update period in minutes (default: 10)"
    )
    
    args = parser.parse_args()
    
    # GitHub Personal Access Token from environment variable
    token = os.getenv("GITHUB_PAT")
    
    if not token:
        raise ValueError("GITHUB_PAT environment variable is not set.")
    
    # Validate period
    if args.period <= 0:
        raise ValueError("Period must be greater than 0")
    
    # Run the agent periodically
    run_periodically(token, args.period)


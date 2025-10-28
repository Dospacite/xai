import os
import time
import signal
import sys
import argparse
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import urlparse
import hashlib

from dotenv import load_dotenv
from pymongo import MongoClient
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from bs4 import BeautifulSoup
import requests

# Load environment variables
load_dotenv()

# Global flag for graceful shutdown
running = True


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    global running
    print("\nShutting down gracefully...")
    running = False
    sys.exit(0)


def get_mongo_client():
    """Connect to MongoDB using Docker hostname."""
    mongo_host = os.getenv("MONGO_HOST", "mongodb")  # Use "mongodb" for Docker, "localhost" for local
    mongo_port = int(os.getenv("MONGO_PORT", "27017"))
    mongo_uri = f"mongodb://{mongo_host}:{mongo_port}/"
    
    client = MongoClient(mongo_uri)
    return client


def setup_chrome_driver():
    """Set up Chrome driver with appropriate options for Docker."""
    chrome_options = Options()
    chrome_options.add_argument("--headless")  # Run in headless mode
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-plugins")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    
    # Additional options for Docker environment
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    
    try:
        # Use the ChromeDriver installed in the Docker container
        driver = webdriver.Chrome(options=chrome_options)
        return driver
    except Exception as e:
        print(f"Failed to initialize Chrome driver: {e}")
        return None


def add_visited_field_to_existing_documents():
    """Add visited field to all existing documents that don't have it."""
    try:
        client = get_mongo_client()
        db = client.phishing_db
        collection = db.phishing_feed
        
        # Update all documents that don't have the visited field
        result = collection.update_many(
            {"visited": {"$exists": False}},
            {"$set": {"visited": False}}
        )
        
        print(f"Added visited field to {result.modified_count} existing documents")
        client.close()
        
    except Exception as e:
        print(f"Error adding visited field: {e}")


def get_non_visited_urls() -> List[Dict]:
    """Get all URLs that haven't been visited yet."""
    try:
        client = get_mongo_client()
        db = client.phishing_db
        collection = db.phishing_feed
        
        # Find documents where visited is False or doesn't exist
        non_visited = list(collection.find(
            {"$or": [{"visited": False}, {"visited": {"$exists": False}}]},
            {"url": 1, "_id": 1}
        ))
        
        client.close()
        return non_visited
        
    except Exception as e:
        print(f"Error getting non-visited URLs: {e}")
        return []


def generate_screenshot_filename(url: str) -> str:
    """Generate a safe filename for the screenshot."""
    # Parse URL to get domain
    parsed_url = urlparse(url)
    domain = parsed_url.netloc.replace("www.", "")
    
    # Create a hash of the full URL for uniqueness
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    
    # Create timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Clean domain name for filename
    safe_domain = "".join(c for c in domain if c.isalnum() or c in ".-_")[:50]
    
    return f"{safe_domain}_{url_hash}_{timestamp}.png"


def fetch_website_content(url: str, driver: webdriver.Chrome) -> Optional[Dict]:
    """Fetch HTML content and take screenshot of a website."""
    error_message = None
    
    try:
        print(f"Visiting: {url}")
        
        # Navigate to the URL
        driver.get(url)
        
        # Wait for page to load (basic wait for dynamic content)
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            error_message = f"Timeout waiting for page to load: {url}"
            print(error_message)
        
        print(f"Page loaded for: {url}")
        
        # Wait 10 seconds after visiting before saving HTML content
        print(f"Waiting 10 seconds before saving content for: {url}")
        time.sleep(10)
        
        # Get page source (HTML) after the 10-second wait
        html_content = driver.page_source
        
        # Parse HTML with BeautifulSoup for cleaner content
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        # Get text content
        text_content = soup.get_text()
        
        print(f"Saving HTML content for: {url}")
        
        # Take screenshot after saving HTML content
        screenshot_filename = generate_screenshot_filename(url)
        screenshot_path = os.path.join("data", "screenshots", screenshot_filename)
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        
        # Take screenshot
        print(f"Taking screenshot for: {url}")
        driver.save_screenshot(screenshot_path)
        
        return {
            "url": url,
            "html_content": html_content,
            "text_content": text_content,
            "screenshot_path": screenshot_path,
            "screenshot_filename": screenshot_filename,
            "fetched_at": datetime.utcnow(),
            "title": soup.title.string if soup.title else "",
            "meta_description": "",
            "status": "success",
            "error": None
        }
        
    except WebDriverException as e:
        error_message = f"WebDriver error: {str(e)}"
        print(f"WebDriver error for {url}: {e}")
        return {
            "url": url,
            "html_content": "",
            "text_content": "",
            "screenshot_path": "",
            "screenshot_filename": "",
            "fetched_at": datetime.utcnow(),
            "title": "",
            "meta_description": "",
            "status": "error",
            "error": error_message
        }
    except Exception as e:
        error_message = f"General error: {str(e)}"
        print(f"Error fetching content for {url}: {e}")
        return {
            "url": url,
            "html_content": "",
            "text_content": "",
            "screenshot_path": "",
            "screenshot_filename": "",
            "fetched_at": datetime.utcnow(),
            "title": "",
            "meta_description": "",
            "status": "error",
            "error": error_message
        }


def save_content_to_db(content_data: Dict):
    """Save fetched content to MongoDB."""
    try:
        client = get_mongo_client()
        db = client.phishing_db
        collection = db.website_content
        
        # Insert the content data
        result = collection.insert_one(content_data)
        print(f"Saved content for {content_data['url']} to database")
        
        client.close()
        return result.inserted_id
        
    except Exception as e:
        print(f"Error saving content to database: {e}")
        return None


def mark_url_as_visited(url: str):
    """Mark a URL as visited in the phishing_feed collection."""
    try:
        client = get_mongo_client()
        db = client.phishing_db
        collection = db.phishing_feed
        
        # Update the document to mark as visited
        result = collection.update_one(
            {"url": url},
            {"$set": {"visited": True, "visited_at": datetime.utcnow()}}
        )
        
        if result.modified_count > 0:
            print(f"Marked {url} as visited")
        else:
            print(f"No document found for URL: {url}")
        
        client.close()
        
    except Exception as e:
        print(f"Error marking URL as visited: {e}")


def process_all_websites():
    """Process all non-visited websites continuously."""
    print("Processing all non-visited websites...")
    
    # Get all non-visited URLs
    non_visited_urls = get_non_visited_urls()
    
    if not non_visited_urls:
        print("No non-visited URLs found")
        return
    
    print(f"Found {len(non_visited_urls)} non-visited URLs to process")
    
    # Set up Chrome driver
    driver = setup_chrome_driver()
    if not driver:
        print("Failed to initialize Chrome driver. Skipping processing.")
        return
    
    try:
        for i, url_data in enumerate(non_visited_urls, 1):
            if not running:
                break
                
            url = url_data["url"]
            print(f"\n--- Processing website {i}/{len(non_visited_urls)}: {url} ---")
            
            # Fetch content and take screenshot
            content_data = fetch_website_content(url, driver)
            
            if content_data:
                # Save content to database
                save_content_to_db(content_data)
                
                # Mark URL as visited
                mark_url_as_visited(url)
                
                print(f"Completed processing: {url}")
            else:
                print(f"Failed to process: {url}")
            
            # No delay between visits - process immediately
            print(f"Moving to next website...")
            
    finally:
        driver.quit()
    
    print(f"Finished processing all {len(non_visited_urls)} websites")


def run_periodically(period_minutes: int = 10):
    """Run the website content fetcher periodically - query for non-visited websites every 10 minutes."""
    global running
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    period_seconds = period_minutes * 60
    
    print(f"Starting website content fetcher (checking for non-visited websites every {period_minutes} minutes)")
    print(f"Press Ctrl+C to stop")
    
    # Add visited field to existing documents on startup
    print("Adding visited field to existing documents...")
    add_visited_field_to_existing_documents()
    
    iteration = 0
    
    while running:
        iteration += 1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            print(f"\n[{timestamp}] Iteration {iteration}: Checking for non-visited websites...")
            process_all_websites()
            print(f"[{timestamp}] Iteration {iteration} completed successfully")
            
        except Exception as e:
            print(f"[{timestamp}] Iteration {iteration} failed: {e}")
        
        if running:
            print(f"Waiting {period_minutes} minutes until next check for non-visited websites...")
            # Sleep in small increments to allow for graceful shutdown
            for _ in range(period_seconds):
                if not running:
                    break
                time.sleep(1)


if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Website content fetcher and screenshot taker"
    )
    parser.add_argument(
        "--period",
        type=int,
        default=10,
        help="Check period for non-visited websites in minutes (default: 10)"
    )
    
    args = parser.parse_args()
    
    # Validate arguments
    if args.period <= 0:
        raise ValueError("Period must be greater than 0")
    
    time.sleep(30) # wait for 30 seconds to let the database be populated

    # Run the fetcher periodically
    run_periodically(args.period)

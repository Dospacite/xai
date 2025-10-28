#!/usr/bin/env python3
"""
Startup script to run both main.py and fetch_website_content.py concurrently in Docker.
"""

import os
import sys
import subprocess
import signal
import time
from multiprocessing import Process
import argparse

# Global flag for graceful shutdown
running = True


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    global running
    print("\nShutting down gracefully...")
    running = False
    sys.exit(0)


def run_main_script(period_minutes):
    """Run the main.py script (phishing feed fetcher)."""
    try:
        print("Starting phishing feed fetcher (main.py)...")
        subprocess.run([
            sys.executable, "main.py", 
            "--period", str(period_minutes)
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running main.py: {e}")
    except KeyboardInterrupt:
        print("main.py interrupted")


def run_fetch_script(period_minutes):
    """Run the fetch_website_content.py script."""
    try:
        print("Starting website content fetcher (fetch_website_content.py)...")
        subprocess.run([
            sys.executable, "fetch_website_content.py",
            "--period", str(period_minutes)
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running fetch_website_content.py: {e}")
    except KeyboardInterrupt:
        print("fetch_website_content.py interrupted")


def main():
    """Main function to start both scripts."""
    global running
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Startup script for phishing feed and website content fetchers"
    )
    parser.add_argument(
        "--main-period",
        type=int,
        default=10,
        help="Update period for main.py in minutes (default: 10)"
    )
    parser.add_argument(
        "--fetch-period",
        type=int,
        default=30,
        help="Update period for fetch_website_content.py in minutes (default: 30)"
    )
    
    args = parser.parse_args()
    
    print("Starting both phishing feed fetcher and website content fetcher...")
    print(f"Main script period: {args.main_period} minutes")
    print(f"Fetch script period: {args.fetch_period} minutes")
    print("Press Ctrl+C to stop both scripts")
    
    # Start both scripts as separate processes
    main_process = Process(
        target=run_main_script,
        args=(args.main_period,)
    )
    
    fetch_process = Process(
        target=run_fetch_script,
        args=(args.fetch_period,)
    )
    
    try:
        # Start both processes
        main_process.start()
        fetch_process.start()
        
        print("Both scripts started successfully")
        
        # Wait for both processes to complete
        main_process.join()
        fetch_process.join()
        
    except KeyboardInterrupt:
        print("\nReceived interrupt signal, stopping both scripts...")
        
        # Terminate both processes
        if main_process.is_alive():
            main_process.terminate()
        if fetch_process.is_alive():
            fetch_process.terminate()
        
        # Wait for processes to terminate
        main_process.join(timeout=5)
        fetch_process.join(timeout=5)
        
        # Force kill if still alive
        if main_process.is_alive():
            main_process.kill()
        if fetch_process.is_alive():
            fetch_process.kill()
        
        print("Both scripts stopped")
    
    except Exception as e:
        print(f"Error running scripts: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

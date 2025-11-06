#!/usr/bin/env python3
"""
Simplified startup script to run main.py and fetch_website_content.py concurrently.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from typing import List

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CHILD_PROCS: List[subprocess.Popen] = []


def start_child(script: str, period: int) -> subprocess.Popen:
    """Start a child Python script with the given period and return the Popen object."""
    cmd = [sys.executable, script, "--period", str(period)]
    logging.info("Starting %s (period=%s)", script, period)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def terminate_children(timeout: float = 5.0):
    """Attempt graceful shutdown, then force kill if needed."""
    if not CHILD_PROCS:
        return

    logging.info("Terminating child processes...")
    for p in CHILD_PROCS:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if all((p.poll() is not None) for p in CHILD_PROCS):
            break
        time.sleep(0.1)

    # Force kill any remaining
    for p in CHILD_PROCS:
        if p.poll() is None:
            logging.warning("Killing unresponsive child pid=%s", p.pid)
            try:
                p.kill()
            except Exception:
                pass


def forward_output(proc: subprocess.Popen, name: str):
    """Non-blocking-ish drain of stdout (called infrequently in the monitor loop)."""
    if proc.stdout:
        # read available lines without blocking - .readline() will block if nothing
        # so use .read() limited to avoid blocking too long; here small chunk
        try:
            out = proc.stdout.read()
            if out:
                for line in out.splitlines():
                    logging.info("[%s] %s", name, line)
        except Exception:
            # If the OS buffer doesn't support non-blocking read, ignore
            pass


def main():
    parser = argparse.ArgumentParser(description="Startup both fetchers concurrently")
    parser.add_argument("--main-period", type=int, default=10, help="main.py period in minutes")
    parser.add_argument("--fetch-period", type=int, default=30, help="fetch_website_content.py period in minutes")
    args = parser.parse_args()

    # Validate scripts exist (optional but helpful)
    scripts = [("main.py", args.main_period), ("fetch_website_content.py", args.fetch_period)]
    for name, _ in scripts:
        if not os.path.exists(name):
            logging.warning("Script %s not found in working directory", name)

    # Start children
    try:
        CHILD_PROCS.extend([
            start_child("main.py", args.main_period),
            start_child("fetch_website_content.py", args.fetch_period),
        ])

        # Setup signal handlers to do graceful shutdown
        def _signal_handler(signum, frame):
            logging.info("Received signal %s, shutting down...", signum)
            terminate_children()
            sys.exit(0)

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        # Monitor loop: poll children, forward a bit of output, exit if both exit
        while True:
            alive = [p for p in CHILD_PROCS if p.poll() is None]
            for p, name in zip(CHILD_PROCS, ("main", "fetch")):
                if p.poll() is None:
                    forward_output(p, name)
                else:
                    # drain remaining output once after exit
                    forward_output(p, name)

            if not alive:
                logging.info("All child processes exited.")
                break

            time.sleep(0.5)

    except SystemExit:
        raise
    except Exception as e:
        logging.exception("Unexpected error in startup script: %s", e)
    finally:
        terminate_children()


if __name__ == "__main__":
    main()

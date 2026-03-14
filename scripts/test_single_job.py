#!/usr/bin/env python3
"""Simple test script for creating a single download job and checking status."""

import time

import requests

import os

API_URL = os.environ.get("NUGS_API_URL", "http://127.0.0.1:8090")


def main():
    requests.post(f"{API_URL}/config", json={"max_concurrent_jobs": 2}).raise_for_status()
    out_path = f"test-output-single-{int(time.time())}"

    # Example URL (will be normalized by the server: /watch/ will be stripped)
    payload = {
        "urls": ["https://play.nugs.net/watch/release/38966"],
        "download_audio": False,
        "download_video": True,
        "video_format": 1,
        "out_path": out_path,
    }

    r = requests.post(f"{API_URL}/jobs", json=payload)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    print("Created job:", job_id)

    while True:
        time.sleep(3)
        r = requests.get(f"{API_URL}/jobs/{job_id}")
        r.raise_for_status()
        status = r.json()
        print(status)
        if status["status"] in ("success", "failed", "cancelled"):
            break

    r = requests.get(f"{API_URL}/jobs/{job_id}/logs", params={"lines": 50})
    r.raise_for_status()
    print("--- LOGS ---")
    print("\n".join(r.json()["logs"]))


if __name__ == "__main__":
    main()

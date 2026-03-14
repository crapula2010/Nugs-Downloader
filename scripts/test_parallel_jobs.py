#!/usr/bin/env python3
"""Test script to start multiple jobs in parallel and poll status."""

import time

import requests

import os

API_URL = os.environ.get("NUGS_API_URL", "http://127.0.0.1:8090")


def main():
    requests.post(f"{API_URL}/config", json={"max_concurrent_jobs": 1}).raise_for_status()
    out_path = f"test-output-parallel-{int(time.time())}"

    urls = [
        "https://play.nugs.net/release/38970",
        "https://play.nugs.net/watch/release/38966",
    ]

    job_ids = []
    for u in urls:
        payload = {
            "urls": [u],
            "download_audio": False,
            "download_video": True,
            "video_format": 1,
            "out_path": out_path,
        }
        r = requests.post(f"{API_URL}/jobs", json=payload)
        r.raise_for_status()
        job_ids.append(r.json()["job_id"])

    print("Started jobs:", job_ids)

    r = requests.get(f"{API_URL}/jobs")
    r.raise_for_status()
    print("Initial queue state:", r.json())

    # Poll until all done.
    while True:
        time.sleep(5)
        statuses = []
        for jid in job_ids:
            r = requests.get(f"{API_URL}/jobs/{jid}")
            r.raise_for_status()
            statuses.append((jid, r.json()["status"]))
        print(statuses)
        if all(s in ("success", "failed", "cancelled") for _, s in statuses):
            break

    # Print logs
    for jid in job_ids:
        r = requests.get(f"{API_URL}/jobs/{jid}/logs", params={"lines": 20})
        r.raise_for_status()
        print(f"\nLogs for {jid}:")
        print("\n".join(r.json()["logs"]))

    requests.post(f"{API_URL}/config", json={"max_concurrent_jobs": 2}).raise_for_status()


if __name__ == "__main__":
    main()

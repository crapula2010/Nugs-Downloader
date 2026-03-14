#!/usr/bin/env python3
"""Test script demonstrating cancelling a running job."""

import time

import requests

import os

API_URL = os.environ.get("NUGS_API_URL", "http://127.0.0.1:8090")


def main():
    requests.post(f"{API_URL}/config", json={"max_concurrent_jobs": 1}).raise_for_status()
    out_path = f"test-output-cancel-{int(time.time())}"

    first_payload = {
        "urls": ["https://play.nugs.net/watch/release/38966"],
        "download_audio": False,
        "download_video": True,
        "video_format": 1,
        "out_path": out_path,
    }
    second_payload = {
        "urls": ["https://play.nugs.net/release/38970"],
        "download_audio": False,
        "download_video": True,
        "video_format": 1,
        "out_path": out_path,
    }

    r = requests.post(f"{API_URL}/jobs", json=first_payload)
    r.raise_for_status()
    first_job_id = r.json()["job_id"]
    print("Created first job:", first_job_id)

    r = requests.post(f"{API_URL}/jobs", json=second_payload)
    r.raise_for_status()
    second_job_id = r.json()["job_id"]
    print("Created second job:", second_job_id)

    time.sleep(1)
    r = requests.get(f"{API_URL}/jobs/{second_job_id}")
    r.raise_for_status()
    print("Second job before cancel:", r.json())

    r = requests.post(f"{API_URL}/jobs/{second_job_id}/cancel")
    r.raise_for_status()
    print("Cancelled queued job", second_job_id)

    r = requests.get(f"{API_URL}/jobs/{second_job_id}")
    r.raise_for_status()
    print("Status after cancel:", r.json())

    r = requests.post(f"{API_URL}/jobs/{second_job_id}/delete")
    r.raise_for_status()
    print("Deleted cancelled job", second_job_id)

    requests.post(f"{API_URL}/config", json={"max_concurrent_jobs": 2}).raise_for_status()


if __name__ == "__main__":
    main()

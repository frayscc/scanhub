"""Synthetic uploads only, to an explicitly acknowledged diagnostic receiver."""
import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

# Small valid PNG; no production documents are read by this script.
import base64
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAoAAAAUCAIAAAA7jDsBAAAAHUlEQVR4nGP8//8/A27AhEeOYVQaB2DCJcEwoqUBagcDJbd2MKEAAAAASUVORK5CYII=")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8687/api/debug/upload")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--confirm-test-target", action="store_true", required=True)
    args = parser.parse_args()
    token = os.environ.get("SCANHUB_TEST_TOKEN")
    if not token:
        parser.error("Set SCANHUB_TEST_TOKEN; use an isolated test receiver")
    def upload(index):
        time.sleep(max(args.interval, 0))
        with httpx.Client(timeout=60, trust_env=False) as client:
            response = client.post(args.url, headers={"X-Upload-Token": token},
                                   data={"device": "SIMULATOR", "text": "synthetic-test"},
                                   files=[("paper", (f"test-{index}-{page}.png", PNG, "image/png"))
                                          for page in range(args.pages)])
            print(f"request={index} status={response.status_code}")
            return response.is_success
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(upload, range(args.count)))
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

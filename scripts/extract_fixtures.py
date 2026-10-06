"""Extract GWT-RPC request/response pairs from HAR file for use as test fixtures."""
import json
import sys
from pathlib import Path
from collections import defaultdict

HAR_PATH = Path(__file__).parent.parent / "www.loseit.com.har"
FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"

TARGET_METHODS = [
    "searchFoods",
    "updateFoodLogEntry",
    "getDailyDetailsIncludingPendingForDate",
    "getFood",
    "getUnsavedFoodLogEntry",
    "getInitializationData",
]


def extract():
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    with open(HAR_PATH) as f:
        har = json.load(f)

    entries = har["log"]["entries"]
    saved = defaultdict(int)

    for e in entries:
        url = e["request"]["url"]
        if "/web/service" not in url:
            continue
        if "postData" not in e["request"]:
            continue

        body = e["request"]["postData"].get("text", "")
        parts = body.split("|")
        if len(parts) < 7:
            continue

        method = parts[6]
        if method not in TARGET_METHODS:
            continue

        saved[method] += 1
        n = saved[method]

        req_path = FIXTURES_DIR / f"{method}_request_{n:02d}.txt"
        with open(req_path, "w") as fout:
            fout.write(body)

        resp_text = ""
        content = e["response"]["content"]
        if "text" in content:
            resp_text = content["text"]
        elif content.get("encoding") == "base64" and "text" in content:
            import base64
            resp_text = base64.b64decode(content["text"]).decode("utf-8", errors="replace")

        resp_path = FIXTURES_DIR / f"{method}_response_{n:02d}.txt"
        with open(resp_path, "w") as fout:
            fout.write(resp_text)

        print(f"Extracted {method} #{n}: request={len(body)}b, response={len(resp_text)}b")


if __name__ == "__main__":
    extract()

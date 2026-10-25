"""Writes site/dashboard.json using the bridge's own data code (run by GitHub Actions)."""
import json
import os
import sys

from xauusd_bridge import app


def main():
    data = app.test_client().get("/api/dashboard").get_json()
    core = [data.get(k) for k in ("gold", "dxy", "vix", "us10y", "wti")]
    if not any(core):
        print("All core quotes failed; keeping the previous snapshot.", file=sys.stderr)
        sys.exit(1)
    os.makedirs("site", exist_ok=True)
    with open("site/dashboard.json", "w") as f:
        json.dump(data, f)
    print("ok", data["fetched_utc"], {k: v is not None for k, v in data.items()})


if __name__ == "__main__":
    main()

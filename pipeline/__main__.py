"""CLI: python -m pipeline <lat> <lng> [--force]"""
import json
import sys

from .property import assess

if __name__ == "__main__":
    lat, lng = float(sys.argv[1]), float(sys.argv[2])
    s = assess(lat, lng, progress=lambda m: print("·", m, flush=True), force="--force" in sys.argv)
    print(json.dumps({k: s[k] for k in ("id", "home", "trees", "lidar", "ortho", "notes", "elapsed_s")}, indent=1))

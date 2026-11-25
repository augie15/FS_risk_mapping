"""CLI: python -m pipeline.ai <property_id> <local|claude>"""
import json
import sys

from dotenv import load_dotenv

from pipeline.property import ROOT

from .runner import run

if __name__ == "__main__":
    load_dotenv(ROOT / ".env", override=True)
    r = run(sys.argv[1], sys.argv[2], progress=lambda m: print("·", m, flush=True))
    print(json.dumps(r["totals"], indent=1))
    for z in r["zones"]:
        print(f"\n[{z['zone']}] {z.get('error') or ''} {z['latency_s']}s")
        for p in z.get("problems", []):
            print("   problem:", p)
        for f in z["findings"]:
            print(f"  {f['item_id']:24} {f['status']:9} {f['confidence']:.2f}  lidar={f['lidar']['result']:8} {f['reason'][:90]}")

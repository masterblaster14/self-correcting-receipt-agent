"""Baseline vs self-correcting comparison over the sample receipts (proposal section V.6).

python tools/benchmark.py                 # all images in samples/
python tools/benchmark.py samples/*_bad.jpg --fault

Ground truth: tools/make_samples.py prints the true grand total for each synthetic receipt; those
are recorded in TRUTH below. For real receipts add entries by hand (filename stem -> total).
Prints a markdown table you can paste into the report.
"""
import argparse
import glob
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
env = ROOT / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from app.pipeline.agent import run_pipeline  # noqa: E402

TRUTH = {  # stem -> true grand total (from make_samples.py)
    "cafe_clean": 430, "cafe_clean_bad": 430,
    "restaurant_service": 1026, "restaurant_service_bad": 1026,
    "pharmacy_discount": 277, "pharmacy_discount_bad": 277,
    "grocery_inclusive": 861, "grocery_inclusive_bad": 861,
}

ap = argparse.ArgumentParser()
ap.add_argument("images", nargs="*")
ap.add_argument("--fault", action="store_true", help="inject a fault in both modes (stress test)")
a = ap.parse_args()
files = a.images or sorted(glob.glob(str(ROOT / "samples" / "*.jpg")))

rows = []
for f in files:
    stem = Path(f).stem
    data = Path(f).read_bytes()
    base = run_pipeline(data, self_correct=False, demo_fault=a.fault)
    agent = run_pipeline(data, self_correct=True, max_iterations=3, demo_fault=a.fault)
    truth = TRUTH.get(stem)
    ok = lambda r: ("✓" if truth is not None and r.profile.total is not None and abs(r.profile.total - truth) < 0.01 else ("?" if truth is None else "✗"))
    rows.append((stem, truth, base.profile.total, base.state.value, ok(base), agent.profile.total, agent.state.value, agent.iterations, ok(agent)))
    print(f"{stem:<26} baseline {base.state.value:<18} {base.profile.total!s:>9} {ok(base)}   agent {agent.state.value:<18} {agent.profile.total!s:>9} {ok(agent)} ({agent.iterations} iter)", file=sys.stderr)

print("\n| Receipt | Truth | Baseline total | Baseline state | OK | Agent total | Agent state | Iters | OK |")
print("|---|---|---|---|---|---|---|---|---|")
for r in rows:
    print("| " + " | ".join(str(x) for x in r) + " |")
n = len(rows)
if n:
    print(f"\nBaseline correct: {sum(r[4]=='✓' for r in rows)}/{n} · Agent correct: {sum(r[8]=='✓' for r in rows)}/{n} · "
          f"Agent verified: {sum(r[6]=='VERIFIED' for r in rows)}/{n} · Baseline flagged: {sum(r[3]!='VERIFIED' for r in rows)}/{n}")

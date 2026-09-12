"""Key-field accuracy on ICDAR-2019 SROIE (task 3), baseline vs self-correcting agent.

Expected layout (the common GitHub/Kaggle mirror, e.g. zzzDavid/ICDAR-2019-SROIE):
    <root>/img/X00016469612.jpg
    <root>/key/X00016469612.json     -> {"company": ..., "date": ..., "address": ..., "total": ...}

python tools/eval_sroie.py <root> --n 30 [--seed 0] [--out sroie_results.csv]

Scores: exact match on total (numeric, tolerance 0.01), date match (any common format normalised to
YYYY-MM-DD), and fuzzy match on company (normalised token overlap >= 0.6). Address is reported but
receipts print it in many layouts, so it is not part of the headline number.
"""
import argparse
import csv
import json
import os
import random
import re
import sys
from datetime import datetime
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


def norm_text(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()


def fuzzy(a, b):
    ta, tb = set(norm_text(a)), set(norm_text(b))
    return len(ta & tb) / max(1, len(tb))


DATE_FMTS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y",
             "%d %b %y", "%d/%b/%Y", "%d-%b-%Y", "%d/%b/%y", "%d-%b-%y", "%d %b, %Y", "%Y/%m/%d", "%m/%d/%Y")


def norm_date(s):
    """Normalise the many SROIE ground-truth spellings (15/APR/2017, 24 MAR 18, 2018-03-24 ...)."""
    if not s:
        return None
    s = " ".join(str(s).strip().split()).title()   # 'APR' -> 'Apr' so %b matches
    for fmt in DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def num(s):
    try:
        return float(re.sub(r"[^0-9.]", "", str(s)))
    except ValueError:
        return None


def summarize(rows, seed, out):
    n = len(rows)
    agg = {"base": {"total": 0, "date": 0, "company": 0}, "agent": {"total": 0, "date": 0, "company": 0}}
    flagged = {"base": 0, "agent": 0}
    corrected = 0
    for r in rows:
        for k in ("base", "agent"):
            for fld in ("total", "date", "company"):
                agg[k][fld] += str(r[f"{k}_{fld}_ok"]) == "True"
            flagged[k] += r[f"{k}_state"] != "VERIFIED"
        corrected += int(r["agent_iters"]) > 0
    print(f"\nSROIE key-field accuracy on {n} receipts (seed {seed})\n")
    print("| Metric | Single-pass baseline | Self-correcting agent |")
    print("|---|---|---|")
    for fld in ("total", "date", "company"):
        print(f"| {fld} correct | {agg['base'][fld]}/{n} ({100*agg['base'][fld]/n:.0f}%) | {agg['agent'][fld]}/{n} ({100*agg['agent'][fld]/n:.0f}%) |")
    print(f"| receipts left arithmetically inconsistent (flagged) | {flagged['base']}/{n} | {flagged['agent']}/{n} |")
    print(f"| receipts where a correction pass ran | n/a | {corrected}/{n} |")
    print(f"\nPer-receipt results written to {out}")


def rescore(rows):
    """Recompute the ok columns from stored values (used by --rescore after scorer fixes)."""
    for r in rows:
        for k in ("base", "agent"):
            gt_t = num(r["gt_total"]); t = num(r[f"{k}_total"]) if r[f"{k}_total"] not in ("", "None") else None
            r[f"{k}_total_ok"] = t is not None and gt_t is not None and abs(t - gt_t) < 0.011
            r[f"{k}_date_ok"] = norm_date(r[f"{k}_date"]) == norm_date(r["gt_date"])
            r[f"{k}_company_ok"] = fuzzy(r[f"{k}_company"], r["gt_company"]) >= 0.6
    return rows


ap = argparse.ArgumentParser()
ap.add_argument("root", nargs="?")
ap.add_argument("--n", type=int, default=30)
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--out", default="sroie_results.csv")
ap.add_argument("--rescore", help="re-score an existing results CSV (no model calls)")
a = ap.parse_args()

if a.rescore:
    with open(a.rescore, newline="", encoding="utf-8") as fh:
        rows = rescore(list(csv.DictReader(fh)))
    with open(a.rescore, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    summarize(rows, a.seed, a.rescore)
    sys.exit(0)
if not a.root:
    sys.exit("usage: eval_sroie.py <root> [--n N] | --rescore results.csv")

root = Path(a.root)
imgs = sorted((root / "img").glob("*.jpg"))
random.Random(a.seed).shuffle(imgs)
imgs = imgs[: a.n]
if not imgs:
    sys.exit(f"No images under {root / 'img'}")

rows, agg = [], {"base": {"total": 0, "date": 0, "company": 0}, "agent": {"total": 0, "date": 0, "company": 0}}
for i, f in enumerate(imgs, 1):
    gt = json.loads((root / "key" / (f.stem + ".json")).read_text(encoding="utf-8"))
    data = f.read_bytes()
    res = {"base": run_pipeline(data, self_correct=False), "agent": run_pipeline(data, self_correct=True, max_iterations=3)}
    row = {"file": f.name, "gt_total": gt.get("total"), "gt_date": norm_date(gt.get("date")), "gt_company": gt.get("company")}
    for k, r in res.items():
        p = r.profile
        t_ok = p.total is not None and num(gt.get("total")) is not None and abs(p.total - num(gt.get("total"))) < 0.011
        d_ok = norm_date(p.date) == norm_date(gt.get("date"))
        c_ok = fuzzy(p.vendor_name, gt.get("company")) >= 0.6
        agg[k]["total"] += t_ok; agg[k]["date"] += d_ok; agg[k]["company"] += c_ok
        row.update({f"{k}_total": p.total, f"{k}_total_ok": t_ok, f"{k}_date": p.date, f"{k}_date_ok": d_ok,
                    f"{k}_company": p.vendor_name, f"{k}_company_ok": c_ok, f"{k}_state": r.state.value, f"{k}_iters": r.iterations})
    rows.append(row)
    print(f"[{i}/{len(imgs)}] {f.name}  base total {'OK' if row['base_total_ok'] else 'X'}  agent total {'OK' if row['agent_total_ok'] else 'X'} ({row['agent_iters']} iter)", file=sys.stderr)

with open(a.out, "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)

summarize(rows, a.seed, a.out)

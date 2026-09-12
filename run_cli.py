"""Command-line runner: python run_cli.py samples/receipt.jpg [--no-correct] [--fault] [--pdf out.pdf]"""
import argparse
import json
import os
import sys
from pathlib import Path

env = Path(__file__).parent / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from app.pipeline.agent import run_pipeline  # noqa: E402
from app.pipeline.report import build_pdf  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("image")
ap.add_argument("--no-correct", action="store_true", help="baseline single pass")
ap.add_argument("--fault", action="store_true", help="inject a demo fault after first extraction")
ap.add_argument("--iters", type=int, default=3)
ap.add_argument("--pdf", help="write PDF report here")
ap.add_argument("--json", action="store_true", help="print full profile JSON")
a = ap.parse_args()

res = run_pipeline(Path(a.image).read_bytes(), self_correct=not a.no_correct, max_iterations=a.iters,
                   demo_fault=a.fault, progress=lambda s, m: print(f"  [{s:<10}] {m}", file=sys.stderr))

p = res.profile
print(f"\n== {res.state.value}  ({res.iterations} correction iteration(s), {res.total_ms/1000:.1f}s, model={res.model})")
print(f"   {p.vendor_name} | {p.date} | {p.currency} {p.total} | {res.category}")
print(f"   items={len(p.line_items)} sum={p.items_sum()} subtotal={p.subtotal} tax={p.tax_sum()} discount={p.discount} round={p.round_off}")
print("\n-- checks")
for c in res.verification.checks:
    print(f"   {'PASS' if c.passed else ('FAIL' if c.severity.value=='error' else 'WARN'):4} {c.id} {c.name}: {c.message}")
print("\n-- trace")
for e in res.trace:
    print(f"   [{e.iteration}] {e.state.value:<26} {e.title}: {e.detail}")
    for ch in e.changes:
        print(f"         {ch.field}: {ch.before} -> {ch.after}")
    if e.revision_notes and e.iteration > 0:
        print(f"         agent: {e.revision_notes}")
if a.json:
    print("\n" + json.dumps(p.model_dump(), indent=2, ensure_ascii=False))
if a.pdf:
    import base64
    Path(a.pdf).write_bytes(build_pdf(res, base64.standard_b64decode(res.original_image_b64)))
    print(f"\nPDF written to {a.pdf}")

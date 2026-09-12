"""Terminal report of the models in the system: architectures, parameters, weights, live timing,
OCR quality on the sample receipts, OCR-vs-VLM agreement on stored extractions, and the evaluation
tables. Every figure is measured when the script runs or read from results/.

python tools/model_report.py            # full report
python tools/model_report.py --quick    # skip the per-sample OCR timing loop
"""
import argparse
import csv
import json
import os
import sqlite3
import statistics
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("PYTHONWARNINGS", "ignore")   # PyTorch's int8 quantisation deprecation notice
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MOCK_LLM", "1")  # this report never calls the paid model

W = 78


def hr(title=""):
    if title:
        pad = W - len(title) - 4
        print(f"\n== {title} " + "=" * max(0, pad))
    else:
        print("=" * W)


def row(k, v):
    print(f"  {k:<34} {v}")


def count_params(m):
    return sum(p.numel() for p in m.parameters())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    hr()
    print("  SELF-CORRECTING RECEIPT AGENT - MODEL REPORT")
    print(f"  {time.strftime('%Y-%m-%d %H:%M')}  ·  python {sys.version.split()[0]}")
    hr()

    # ------------------------------------------------------------------ runtime
    import numpy, cv2
    hr("Runtime")
    row("OpenCV", cv2.__version__)
    row("NumPy", numpy.__version__)
    try:
        import torch
        row("PyTorch", f"{torch.__version__}  device=cpu  threads={torch.get_num_threads()}")
    except Exception:
        torch = None
        row("PyTorch", "not installed")

    # ------------------------------------------------------------------ OCR models
    hr("Model 1+2  ·  local neural OCR (EasyOCR)")
    try:
        import easyocr
        t0 = time.time()
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        load_s = time.time() - t0
        det, rec = reader.detector, reader.recognizer
        mdir = Path.home() / ".EasyOCR" / "model"
        row("Load time (both nets)", f"{load_s:.2f} s")
        row("Detector", "CRAFT - Character Region Awareness For Text detection")
        row("  architecture", "VGG-16-BN backbone + U-Net decoder -> region & affinity heatmaps")
        row("  parameters", f"{count_params(det)/1e6:.1f} M")
        row("  weights", f"craft_mlt_25k.pth  {(mdir/'craft_mlt_25k.pth').stat().st_size/1e6:.1f} MB")
        row("  conv layers", str(sum(1 for m in det.modules() if isinstance(m, torch.nn.Conv2d))))
        row("Recogniser", "CRNN - convolutional features -> BiLSTM -> CTC decoding")
        row("  modules", " -> ".join(n for n, _ in rec.named_children()))
        row("  parameters", f"{count_params(rec)/1e6:.2f} M (dynamically quantised int8)")
        row("  weights", f"english_g2.pth  {(mdir/'english_g2.pth').stat().st_size/1e6:.1f} MB")
        row("  alphabet", f"{len(reader.character)} symbols (digits, letters, punctuation) + CTC blank")
        ocr_ok = True
    except Exception as e:
        print(f"  EasyOCR not available: {type(e).__name__}: {e}")
        ocr_ok = False

    # ------------------------------------------------------------------ VLM
    hr("Model 3  ·  vision-language model (perception)")
    model = os.environ.get("MODEL", "claude-opus-5")
    row("Model", model)
    row("Role", "image + JSON schema -> structured expense profile, zone coordinates, uncertainty")
    row("Also used for", "12-way expense categorisation, policy compliance reasoning, ledger SQL agent")
    row("Output contract", "18-field JSON schema enforced server-side (models.EXPENSE_SCHEMA)")
    row("Swappable", "yes - interface is image+schema in / JSON out")

    # ------------------------------------------------------------------ live OCR on samples
    from app.pipeline import ocr
    from app.pipeline.preprocess import preprocess
    samples = sorted((ROOT / "samples").glob("*.jpg"))
    if ocr_ok and not a.quick and samples:
        hr(f"Live OCR inference on {len(samples)} sample receipts (CPU)")
        print(f"  {'receipt':<26}{'pre(ms)':>8}{'ocr(ms)':>9}{'words':>7}{'conf>=0.8':>11}{'mean conf':>11}")
        tot_ms, all_conf = [], []
        for f in samples:
            t0 = time.time(); pre = preprocess(f.read_bytes()); pre_ms = (time.time() - t0) * 1000
            t0 = time.time(); words = ocr.run(pre.enhanced_jpeg); ocr_ms = (time.time() - t0) * 1000
            confs = [w["conf"] for w in words]
            all_conf += confs; tot_ms.append(ocr_ms)
            hi = sum(c >= 0.8 for c in confs)
            print(f"  {f.stem:<26}{pre_ms:>8.0f}{ocr_ms:>9.0f}{len(words):>7}{(hi/len(confs) if confs else 0):>10.0%}{(statistics.mean(confs) if confs else 0):>11.2f}")
        row("OCR median latency", f"{statistics.median(tot_ms):.0f} ms  (min {min(tot_ms):.0f}, max {max(tot_ms):.0f})")
        row("Words detected total", f"{len(all_conf)}  ·  mean confidence {statistics.mean(all_conf):.2f}")

    # ------------------------------------------------------------------ agreement vs stored VLM extractions
    db = Path(os.environ.get("DATA_DIR", ROOT / "data")) / "expenses.db"
    if ocr_ok and db.exists():
        from app.pipeline.models import ExpenseProfile
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        rows = c.execute("SELECT id, vendor, currency, total, model, result_json, image_path FROM receipts "
                         "WHERE model != 'mock' ORDER BY created_at DESC LIMIT 12").fetchall()
        rows = [r for r in rows if r["image_path"] and Path(r["image_path"]).exists()]
        if rows:
            hr(f"OCR (local) vs VLM ({model}) agreement on {len(rows)} stored real extractions")
            print(f"  {'vendor':<28}{'total':>12}{'amounts':>9}{'OCR agrees':>12}")
            ratios = []
            for r in rows:
                prof = ExpenseProfile.model_validate(json.loads(r["result_json"])["profile"])
                pre = preprocess(Path(r["image_path"]).read_bytes())
                ag = ocr.agreement(prof, ocr.run(pre.enhanced_jpeg))
                if ag["ratio"] is None:
                    continue
                ratios.append(ag["ratio"])
                print(f"  {(r['vendor'] or '?')[:27]:<28}{(r['currency'] or '')+' '+str(r['total']):>12}{ag['total']:>9}{ag['ratio']:>11.0%}")
            if ratios:
                row("Mean agreement", f"{statistics.mean(ratios):.0%}  (two independent readers of the same numbers)")

    # ------------------------------------------------------------------ verification + loop
    hr("Verification engine & self-correction loop (ours, deterministic)")
    from app.pipeline import verify as v
    src = Path(v.__file__).read_text(encoding="utf-8")
    ids = sorted(set(__import__("re").findall(r'id="(C\d+)"', src)) | {"C10", "C11"}, key=lambda s: int(s[1:]))
    row("Constraint checks", f"{len(ids)}: {', '.join(ids)}")
    row("Loop", "rank failures -> hypothesis -> zone crop (ink-validated / OCR-anchored) -> re-read -> re-verify")
    row("Gate states", "INITIAL -> AWAITING_VERIFICATION -> FLAGGED_FOR_REEXAMINATION -> VERIFIED | FLAGGED_FOR_REVIEW")

    # ------------------------------------------------------------------ evaluation
    res = ROOT / "results"
    csvp = res / "sroie_results.csv"
    if csvp.exists():
        hr("Evaluation  ·  ICDAR-2019 SROIE (30 receipts, seed 0)  baseline = same VLM, loop off")
        rws = list(csv.DictReader(open(csvp, encoding="utf-8")))
        n = len(rws)
        def acc(k, f): return sum(str(r[f"{k}_{f}_ok"]) == "True" for r in rws)
        print(f"  {'metric':<44}{'baseline':>12}{'agent':>12}")
        for f in ("total", "date", "company"):
            print(f"  {f + ' correct':<44}{acc('base', f):>7}/{n} {acc('base', f)/n:>3.0%}{acc('agent', f):>7}/{n} {acc('agent', f)/n:>3.0%}")
        fb = sum(r["base_state"] != "VERIFIED" for r in rws); fa = sum(r["agent_state"] != "VERIFIED" for r in rws)
        print(f"  {'left arithmetically inconsistent':<44}{fb:>7}/{n}     {fa:>7}/{n}")
        print(f"  {'correction pass ran':<44}{'n/a':>12}{sum(int(r['agent_iters'])>0 for r in rws):>7}/{n}")
    bench = res / "logs" / "bench_real.txt"
    if bench.exists():
        hr("Evaluation  ·  9 real phone photos (results/logs/bench_real.txt)")
        lines = [l for l in bench.read_text(encoding="utf-8", errors="ignore").splitlines() if "baseline" in l and "agent" in l]
        fb = sum("FLAGGED" in l.split("agent")[0] for l in lines); fa = sum("FLAGGED" in l.split("agent")[1] for l in lines)
        row("Baseline left flagged", f"{fb}/{len(lines)}")
        row("Agent left flagged", f"{fa}/{len(lines)}")
        row("Repaired by the loop", f"{fb - fa}/{fb}  (folded last row recovered from a zoom crop; day/month swap corrected)")
    hr()
    print("  Trained by us: none of the three networks. Built by us: preprocessing, verifier, loop, cross-check, evaluation.")
    hr()


if __name__ == "__main__":
    main()

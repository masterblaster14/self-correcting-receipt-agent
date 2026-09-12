"""Show the SROIE dataset: statistics in the terminal and an annotated contact sheet image.

python tools/show_sroie.py                 # stats + sheet of 6 random receipts -> data/sroie_sheet.jpg
python tools/show_sroie.py --n 9 --seed 3  # different receipts
python tools/show_sroie.py --id 146        # one receipt, full size, with its boxes and key fields

Dataset: ICDAR 2019 Robust Reading Challenge on Scanned Receipts OCR and Information Extraction
(SROIE). Each receipt has: img/XXX.jpg (scan), box/XXX.csv (every text line: 8 corner coordinates
+ transcription), key/XXX.json (ground truth for company, date, address, total).
"""
import argparse
import json
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "datasets" / "sroie"


def load_boxes(stem):
    rows = []
    p = DS / "box" / f"{stem}.csv"
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split(",", 8)
        if len(parts) < 9:
            continue
        try:
            pts = [int(float(v)) for v in parts[:8]]
        except ValueError:
            continue
        rows.append((pts, parts[8]))
    return rows


def annotate(stem, max_h=900):
    img = cv2.imread(str(DS / "img" / f"{stem}.jpg"))
    key = json.loads((DS / "key" / f"{stem}.json").read_text(encoding="utf-8"))
    boxes = load_boxes(stem)
    for pts, text in boxes:
        poly = np.array(pts, np.int32).reshape(-1, 1, 2)
        t = text.lower()
        color = (0, 0, 220) if any(k in t for k in ("total", "amount")) else (0, 160, 0) if any(ch.isdigit() for ch in text) else (200, 120, 0)
        cv2.polylines(img, [poly], True, color, 2)
    h, w = img.shape[:2]
    s = max_h / h
    img = cv2.resize(img, (int(w * s), max_h))
    # ground-truth strip under the image
    strip = np.full((118, img.shape[1], 3), 255, np.uint8)
    lines = [f"#{stem}  {len(boxes)} text lines", f"company: {key.get('company','')[:40]}",
             f"date: {key.get('date','')}    total: {key.get('total','')}", f"address: {key.get('address','')[:46]}"]
    for i, l in enumerate(lines):
        cv2.putText(strip, l, (8, 24 + 24 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 1, cv2.LINE_AA)
    return np.vstack([img, strip])


def stats():
    imgs = sorted((DS / "img").glob("*.jpg"))
    keys = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((DS / "key").glob("*.json"))]
    sizes = [p.stat().st_size for p in imgs]
    dims = []
    for p in random.Random(0).sample(imgs, min(60, len(imgs))):
        im = cv2.imread(str(p)); dims.append(im.shape[:2])
    nlines = [len(load_boxes(p.stem)) for p in imgs]
    totals = []
    for k in keys:
        try: totals.append(float(str(k.get("total", "")).replace(",", "")))
        except ValueError: pass
    import re
    def year_of(s):
        s = str(s)
        m = re.search(r"(20\d\d)", s)
        if m:
            return m.group(1)
        m = re.search(r"[/\-. ](\d{2})\s*$", s)          # dd/mm/yy, dd-mm-yy, dd mon yy
        return f"20{m.group(1)}" if m and 10 <= int(m.group(1)) <= 25 else "other"
    years = Counter(year_of(k.get("date", "")) for k in keys if k.get("date"))
    print("ICDAR-2019 SROIE  (Scanned Receipts OCR and Information Extraction)")
    print(f"  location        {DS}")
    print(f"  receipts        {len(imgs)} scanned images ({sum(sizes)/1e6:.0f} MB)")
    print(f"  image size      median {int(statistics.median(h for h,_ in dims))} x {int(statistics.median(w for _,w in dims))} px (h x w)")
    print(f"  annotations     box/: {len(nlines)} files, {sum(nlines)} text lines total, median {int(statistics.median(nlines))} lines per receipt")
    print(f"                  key/: {len(keys)} files, fields = company, date, address, total")
    print(f"  totals          median {statistics.median(totals):.2f}, max {max(totals):.2f} (Malaysian ringgit)")
    print(f"  years           {dict(sorted(years.items()))}")
    print(f"  used here       30 receipts, random seed 0, baseline vs agent -> results/sroie_results.csv")
    print(f"  source          ICDAR 2019 RRC task 1-3; mirror github.com/zzzDavid/ICDAR-2019-SROIE")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--id", help="single receipt stem, e.g. 146")
    ap.add_argument("--out", default=str(ROOT / "data" / "sroie_sheet.jpg"))
    a = ap.parse_args()
    if not (DS / "img").exists():
        sys.exit(f"Dataset not found at {DS}. See README (SROIE section).")
    stats()
    if a.id:
        sheet = annotate(a.id, max_h=1400)
    else:
        stems = [p.stem for p in sorted((DS / "img").glob("*.jpg"))]
        pick = random.Random(a.seed).sample(stems, a.n)
        tiles = [annotate(s) for s in pick]
        w = max(t.shape[1] for t in tiles); h = max(t.shape[0] for t in tiles)
        tiles = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, w - t.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255)) for t in tiles]
        cols = 3
        rows = [np.hstack(tiles[i:i + cols] + [np.full((h, w, 3), 255, np.uint8)] * (cols - len(tiles[i:i + cols]))) for i in range(0, len(tiles), cols)]
        sheet = np.vstack(rows)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(a.out, sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"\n  wrote {a.out}   (red = total/amount lines, green = lines with digits, orange = other text)")
    try:
        import os; os.startfile(a.out)  # opens in the default Windows image viewer
    except Exception:
        pass


if __name__ == "__main__":
    main()

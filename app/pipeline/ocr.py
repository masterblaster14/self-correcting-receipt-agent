"""Block 3 - OCR & spatial layout extraction with a local neural OCR engine (EasyOCR).

EasyOCR = CRAFT text detector + CRNN recogniser, running on the laptop CPU, no API.
It is an OPTIONAL dependency: `pip install easyocr`. When it is not installed everything else
works unchanged; `available()` is False and the pipeline skips this block.

What the OCR output is used for:
  * word-level boxes drawn in the UI ("OCR boxes" view)
  * an independent second reading of the numbers -> agreement check C11 against the VLM profile
  * keyword-anchored zones ("total", "gst", ...) used to place re-examination crops when the
    vision model's own zone coordinates fail the ink test
"""
from __future__ import annotations

import re
import threading
from typing import Dict, List, Optional

import cv2
import numpy as np

ENGINE_NAME = "EasyOCR (CRAFT detector + CRNN recogniser, CPU)"
MAX_WORDS = 400
_lock = threading.Lock()
_reader = None
_import_error: Optional[str] = None

try:  # optional dependency
    import easyocr  # type: ignore

    _HAS = True
except Exception as e:  # pragma: no cover
    easyocr = None
    _HAS = False
    _import_error = f"{type(e).__name__}: {e}"


def available() -> bool:
    return _HAS


def _get_reader():
    global _reader
    with _lock:
        if _reader is None:
            # first call downloads ~100 MB of weights into ~/.EasyOCR, later calls are instant
            _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        return _reader


def run(jpeg: bytes) -> List[Dict]:
    """OCR an image. Returns words with normalised boxes (x, y, w, h in 0-1 of the image) and
    recogniser confidence, reading order top-to-bottom."""
    if not _HAS:
        return []
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return []
    H, W = img.shape[:2]
    # EasyOCR handles the resize internally; passing the greyscale enhanced image works best on receipts
    results = _get_reader().readtext(img, detail=1, paragraph=False, width_ths=0.5, add_margin=0.05)
    words = []
    for box, text, conf in results:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, x1, y0, y1 = max(0, min(xs)), min(W, max(xs)), max(0, min(ys)), min(H, max(ys))
        if x1 - x0 < 2 or y1 - y0 < 2 or not str(text).strip():
            continue
        words.append({
            "text": str(text).strip(), "conf": round(float(conf), 3),
            "x": round(x0 / W, 4), "y": round(y0 / H, 4), "w": round((x1 - x0) / W, 4), "h": round((y1 - y0) / H, 4),
        })
    words.sort(key=lambda w: (round(w["y"], 2), w["x"]))
    return words[:MAX_WORDS]


# ------------------------------------------------------------------ numbers: OCR vs VLM agreement
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _norm_num(s: str) -> Optional[float]:
    try:
        return round(float(s.replace(",", "")), 2)
    except ValueError:
        return None


def ocr_numbers(words: List[Dict]) -> set:
    """Every numeric token the OCR engine saw, normalised (so '1,367.00' and '1367' agree).
    Also adds each token divided by 100 for receipts where the recogniser drops the decimal point."""
    nums = set()
    for w in words:
        for m in _NUM_RE.findall(w["text"]):
            v = _norm_num(m)
            if v is None:
                continue
            nums.add(v)
            if "." not in m and len(m.replace(",", "")) >= 3:
                nums.add(round(v / 100, 2))
    return nums


def profile_numbers(profile) -> List[tuple[str, float]]:
    """The money values the vision model committed to, labelled."""
    out = []
    if profile.total is not None:
        out.append(("total", round(profile.total, 2)))
    if profile.subtotal is not None:
        out.append(("subtotal", round(profile.subtotal, 2)))
    for t in profile.taxes:
        if t.amount:
            out.append((f"tax {t.label}", round(t.amount, 2)))
    for i, li in enumerate(profile.line_items):
        if li.amount:
            out.append((f"item {i + 1} {li.description[:18]}", round(li.amount, 2)))
    return out


def agreement(profile, words: List[Dict]) -> Dict:
    """Fraction of the VLM's numbers that the OCR engine independently read somewhere on the receipt."""
    seen = ocr_numbers(words)
    checked = profile_numbers(profile)
    if not checked:
        return {"ratio": None, "matched": 0, "total": 0, "missing": []}
    missing = [f"{label} = {v:,.2f}" for label, v in checked if v not in seen]
    return {"ratio": round(1 - len(missing) / len(checked), 3), "matched": len(checked) - len(missing),
            "total": len(checked), "missing": missing[:8]}


# ------------------------------------------------------------------ keyword-anchored zones
ZONE_KEYWORDS = {
    "total": ("total", "grand total", "net amount", "amount payable", "amount due", "payable", "balance due", "ttl"),
    "taxes": ("gst", "cgst", "sgst", "igst", "vat", "tax", "btw", "service charge", "srv chg", "discount", "round"),
    "header": ("invoice", "bill no", "receipt", "date", "gstin", "tax invoice", "token"),
    "line_items": ("qty", "rate", "amount", "item", "description", "price"),
}


def keyword_zone(words: List[Dict], zone: str, pad: float = 0.04) -> Optional[tuple[float, float, float, float]]:
    """Box (x, y, w, h) spanning the full width around the rows whose text matches the zone keywords."""
    keys = ZONE_KEYWORDS.get(zone, ())
    ys = []
    for w in words:
        t = w["text"].lower()
        if any(k in t for k in keys):
            ys.append((w["y"], w["y"] + w["h"]))
    if not ys:
        return None
    y0, y1 = min(a for a, _ in ys), max(b for _, b in ys)
    if zone == "line_items":   # the items are below the column header row
        y1 = min(1.0, y1 + 0.35)
    y0, y1 = max(0.0, y0 - pad), min(1.0, y1 + pad)
    return (0.0, y0, 1.0, y1 - y0)

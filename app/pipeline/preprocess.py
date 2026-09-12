"""Block 2 - Image preprocessing (OpenCV / Pillow).

Normalises a phone photo so the vision model sees the clearest possible receipt:
EXIF auto-orientation, resize, denoise, deskew, contrast enhancement (CLAHE), and an
adaptive-threshold "OCR view" that is kept for display.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageOps

try:  # iPhone HEIC support if the optional package is present
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

MAX_SIDE = 1600


@dataclass
class Preprocessed:
    original_jpeg: bytes            # oriented, resized original (what the user shot)
    enhanced_jpeg: bytes            # grayscale, deskewed, contrast enhanced -> sent to the model
    binary_jpeg: bytes              # adaptive threshold view for display
    info: dict = field(default_factory=dict)


def _to_jpeg(img: np.ndarray, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


def load_image(data: bytes) -> np.ndarray:
    """Decode any browser/phone image (JPEG/PNG/WebP/HEIC) honouring EXIF rotation."""
    pil = Image.open(io.BytesIO(data))
    pil = ImageOps.exif_transpose(pil).convert("RGB")
    arr = np.array(pil)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _resize(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def _estimate_skew(gray: np.ndarray, max_deg: float = 12.0) -> float:
    """Estimate text skew by projection profile: rotate an ink mask through candidate angles and
    keep the one whose horizontal projection is 'peakiest' (text rows aligned -> high variance).
    The ink mask comes from a local adaptive threshold, so uniform backgrounds and borders do not
    count as ink (the failure mode of a global Otsu threshold)."""
    h, w = gray.shape
    scale = 700.0 / max(h, w)
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else gray
    ink = cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))  # drop 1-px edge lines
    if cv2.countNonZero(ink) < 200:
        return 0.0
    ink = (ink > 0).astype(np.float32)
    H, W = ink.shape
    centre = (W / 2, H / 2)

    def score(a: float) -> float:
        m = cv2.getRotationMatrix2D(centre, a, 1.0)
        r = cv2.warpAffine(ink, m, (W, H), flags=cv2.INTER_NEAREST, borderValue=0)
        return float(np.var(r.sum(axis=1)))

    best, best_s = 0.0, score(0.0)
    for a in np.arange(-max_deg, max_deg + 0.01, 1.0):          # coarse
        s = score(float(a))
        if s > best_s:
            best, best_s = float(a), s
    for a in np.arange(best - 1.0, best + 1.01, 0.2):            # fine
        s = score(float(a))
        if s > best_s:
            best, best_s = float(a), s
    return round(best, 2)


def _rotate(img: np.ndarray, angle: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def crop_region(jpeg: bytes, x: float, y: float, w: float, h: float, pad: float = 0.04, min_width: int = 900) -> bytes:
    """Cut a normalised region out of an image, with padding, and upscale it so small digits get
    more pixels. This is the 'zoom in on the suspect area' primitive of the correction loop."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_UNCHANGED)
    H, W = img.shape[:2]
    x0 = max(0, int((x - pad) * W)); y0 = max(0, int((y - pad) * H))
    x1 = min(W, int((x + w + pad) * W)); y1 = min(H, int((y + h + pad) * H))
    if x1 - x0 < 20 or y1 - y0 < 20:
        x0, y0, x1, y1 = 0, 0, W, H
    crop = img[y0:y1, x0:x1]
    if crop.shape[1] < min_width:
        s = min_width / crop.shape[1]
        crop = cv2.resize(crop, (int(crop.shape[1] * s), int(crop.shape[0] * s)), interpolation=cv2.INTER_CUBIC)
    return _to_jpeg(crop, quality=92)


def _find_receipt(img: np.ndarray):
    """Locate the bright paper region in a handheld photo. Returns (x0, y0, x1, y1) or None.
    Conservative: only crops when one bright blob clearly dominates and is neither the whole
    frame nor a sliver, so dark receipts and tight scans are left untouched."""
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (0, 0), 3)
    thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25))
    thr = cv2.morphologyEx(thr, cv2.MORPH_CLOSE, k)
    thr = cv2.morphologyEx(thr, cv2.MORPH_OPEN, k)
    cnts, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(c)
    frac = (w * h) / float(W * H)
    if not (0.08 < frac < 0.85):
        return None
    # the blob should be mostly filled (a paper sheet), not a thin ring or scattered highlights
    if cv2.contourArea(c) / float(w * h) < 0.6:
        return None
    m = int(0.03 * max(W, H))
    return max(0, x - m), max(0, y - m), min(W, x + w + m), min(H, y + h + m)


def ink_fraction(jpeg: bytes) -> float:
    """Share of dark (ink) pixels in an image - used to sanity-check model-proposed regions."""
    g = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    if g is None or g.size == 0:
        return 0.0
    thr = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    return float((thr > 0).mean())


def preprocess(data: bytes) -> Preprocessed:
    img = _resize(load_image(data))
    info: dict = {"width": int(img.shape[1]), "height": int(img.shape[0])}

    # receipt-aware crop: a handheld shot spends most pixels on table/hand; cropping to the paper
    # gives the model 2-3x more pixels per character
    box = _find_receipt(img)
    if box:
        x0, y0, x1, y1 = box
        img = img[y0:y1, x0:x1]
        img = _resize(img) if max(img.shape[:2]) < MAX_SIDE else img
        info["receipt_crop"] = [int(x0), int(y0), int(x1), int(y1)]
        info["width"], info["height"] = int(img.shape[1]), int(img.shape[0])

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # noise reduction that keeps edges (thermal-paper speckle, JPEG noise)
    den = cv2.fastNlMeansDenoising(gray, None, h=7, templateWindowSize=7, searchWindowSize=21)

    # deskew only when the estimate is small and plausible; big angles are usually a
    # mis-estimate on cluttered backgrounds and would make things worse
    angle = _estimate_skew(den)
    if 0.3 <= abs(angle) <= 12.0:
        den = _rotate(den, angle)
        img = _rotate(img, angle)
        info["deskew_deg"] = round(angle, 2)
    else:
        info["deskew_deg"] = 0.0

    # local contrast enhancement handles uneven lighting / shadows
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(den)

    # binary "OCR view" - adaptive threshold copes with gradients across the receipt
    binary = cv2.adaptiveThreshold(
        enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
    )

    # simple quality signals for the UI
    lap = cv2.Laplacian(gray, cv2.CV_64F).var()
    info["sharpness"] = round(float(lap), 1)
    info["mean_brightness"] = round(float(gray.mean()), 1)
    info["steps"] = ["exif-orient", "resize"] + (["receipt-crop"] if box else []) + ["denoise", "deskew", "clahe", "adaptive-threshold"]

    return Preprocessed(
        original_jpeg=_to_jpeg(img),
        enhanced_jpeg=_to_jpeg(enhanced),
        binary_jpeg=_to_jpeg(binary, quality=80),
        info=info,
    )

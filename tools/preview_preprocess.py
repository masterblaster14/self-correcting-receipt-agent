"""Contact sheet of preprocessing results for every sample: original | enhanced | machine view.

python tools/preview_preprocess.py [out.jpg]
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.pipeline.preprocess import preprocess  # noqa: E402

out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "preprocess_preview.jpg"
tiles = []
for f in sorted((ROOT / "samples").glob("*.jpg")):
    pre = preprocess(f.read_bytes())
    imgs = [cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR) for b in (pre.original_jpeg, pre.enhanced_jpeg, pre.binary_jpeg)]
    h = 320
    row = [cv2.resize(i, (int(i.shape[1] * h / i.shape[0]), h)) for i in imgs]
    strip = np.hstack(row)
    label = f"{f.stem}  crop={pre.info.get('receipt_crop', 'none')}  deskew={pre.info['deskew_deg']}  sharp={pre.info['sharpness']}"
    bar = np.full((28, strip.shape[1], 3), 255, np.uint8)
    cv2.putText(bar, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)
    tiles.append(np.vstack([bar, strip]))
    print(label)
w = max(t.shape[1] for t in tiles)
tiles = [np.hstack([t, np.full((t.shape[0], w - t.shape[1], 3), 255, np.uint8)]) for t in tiles]
out.parent.mkdir(parents=True, exist_ok=True)
cv2.imwrite(str(out), np.vstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 80])
print("wrote", out)

"""Render synthetic Indian-style receipts (clean + degraded) into samples/ for testing.

python tools/make_samples.py
"""
import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parents[1] / "samples"
OUT.mkdir(exist_ok=True)


def font(size, bold=False):
    for name in (("consolab.ttf" if bold else "consola.ttf"), ("courbd.ttf" if bold else "cour.ttf")):
        try:
            return ImageFont.truetype(rf"C:\Windows\Fonts\{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def receipt(vendor, addr, items, gst=5.0, discount=0.0, service=0.0, tax_inclusive=False, inv="A-2291", date="10/09/2026"):
    W = 640
    lines = []
    sub = round(sum(q * r for _, q, r in items), 2)
    tax_each = round(sub * gst / 200, 2)
    pre_round = (sub if tax_inclusive else sub + 2 * tax_each) + service - discount
    total = round(pre_round)
    ro = round(total - pre_round, 2)
    H = 360 + 28 * len(items) + (28 if discount else 0) + (28 if service else 0) + (28 if ro else 0)
    img = Image.new("L", (W, H), 245)
    d = ImageDraw.Draw(img)
    y = 24
    def center(t, f):
        nonlocal y
        w = d.textlength(t, font=f)
        d.text(((W - w) / 2, y), t, font=f, fill=20); y += f.size + 8
    def row(l, r, f):
        nonlocal y
        d.text((36, y), l, font=f, fill=20)
        d.text((W - 36 - d.textlength(r, font=f), y), r, font=f, fill=20); y += f.size + 8
    center(vendor, font(30, True)); center(addr, font(18)); center(f"GSTIN 29ABCDE1234F1Z5", font(16))
    y += 6; row(f"Bill No: {inv}", f"Date: {date}", font(18)); row("Time: 13:42", "Cashier: 03", font(18))
    d.line((36, y, W - 36, y), fill=20); y += 10
    row("Item              Qty   Rate", "Amount", font(18, True)); d.line((36, y, W - 36, y), fill=20); y += 10
    for name, q, r in items:
        row(f"{name:<16}{q:>4}{r:>9.2f}", f"{q*r:.2f}", font(18))
    d.line((36, y, W - 36, y), fill=20); y += 10
    row("Sub Total", f"{sub:.2f}", font(18))
    row(f"CGST @ {gst/2:g}%", f"{tax_each:.2f}", font(18)); row(f"SGST @ {gst/2:g}%", f"{tax_each:.2f}", font(18))
    if service: row("Service Charge", f"{service:.2f}", font(18))
    if discount: row("Discount", f"-{discount:.2f}", font(18))
    if ro: row("Round Off", f"{ro:+.2f}", font(18))
    d.line((36, y, W - 36, y), fill=20); y += 10
    row("GRAND TOTAL", f"Rs. {total:.2f}", font(24, True)); y += 6
    if tax_inclusive: center("* Prices are inclusive of GST *", font(16))
    center("Paid by UPI", font(18)); center("Thank you, visit again!", font(18))
    return img, total


def degrade(img: Image.Image, angle=4.0, blur=1.2, noise=18, shadow=True):
    arr = np.array(img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=120)).astype(np.float32)
    if shadow:
        h, w = arr.shape
        grad = np.linspace(0.55, 1.0, w)[None, :]
        arr = arr * grad
    arr += np.random.normal(0, noise, arr.shape)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    out = Image.fromarray(arr).filter(ImageFilter.GaussianBlur(blur))
    return out.convert("RGB")


random.seed(7)
cases = {
    "cafe_clean": receipt("SRI KRISHNA CAFE", "Koramangala, Bengaluru", [("Masala Dosa", 2, 120), ("Filter Coffee", 2, 40), ("Idli Vada", 1, 90)]),
    "restaurant_service": receipt("Punjabi Dhaba", "Sector 18, Noida", [("Dal Makhani", 1, 260), ("Butter Naan", 4, 45), ("Paneer Tikka", 1, 320), ("Lassi", 2, 80)], service=60.0, inv="R-7781"),
    "pharmacy_discount": receipt("Apollo Pharmacy", "Anna Nagar, Chennai", [("Paracetamol 650", 2, 32.5), ("Vitamin C", 1, 145), ("Band Aid", 1, 60)], gst=12.0, discount=25.0, inv="PH-55120", date="03/09/2026"),
    "grocery_inclusive": receipt("More Supermarket", "Baner, Pune", [("Toor Dal 1kg", 1, 165), ("Rice 5kg", 1, 420), ("Milk 1L", 3, 62), ("Bread", 2, 45)], tax_inclusive=True, inv="G-30931"),
}
for name, (img, total) in cases.items():
    img.convert("RGB").save(OUT / f"{name}.jpg", quality=92)
    degrade(img).save(OUT / f"{name}_bad.jpg", quality=78)
    print(f"{name}: total {total}")
print(f"\nwritten to {OUT}")

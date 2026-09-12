"""Block 10 - Auto-generate the PDF expense report (reportlab)."""
from __future__ import annotations

import io
import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .models import ProcessResult, ReceiptState

# Helvetica has no rupee glyph; use Arial on Windows / DejaVu on Linux when available.
_FONT = "Helvetica"
_FONT_B = "Helvetica-Bold"
for regular, bold, name in (
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf", "Arial"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu"),
):
    if os.path.exists(regular) and os.path.exists(bold):
        pdfmetrics.registerFont(TTFont(name, regular))
        pdfmetrics.registerFont(TTFont(name + "-Bold", bold))
        _FONT, _FONT_B = name, name + "-Bold"
        break

SYMBOL = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def money(x, cur: str) -> str:
    if x is None:
        return "—"
    sym = SYMBOL.get(cur, cur + " ") if _FONT != "Helvetica" else (cur + " ")
    return f"{sym}{x:,.2f}"


def build_pdf(res: ProcessResult, original_jpeg: bytes | None = None) -> bytes:
    p = res.profile
    cur = p.currency or "INR"
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName=_FONT_B, fontSize=18, spaceAfter=2, alignment=0)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontName=_FONT, fontSize=9, textColor=colors.HexColor("#667085"))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName=_FONT_B, fontSize=12, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=ss["Normal"], fontName=_FONT, fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11, textColor=colors.HexColor("#475467"))

    verified = res.state == ReceiptState.verified
    badge_color = colors.HexColor("#12B76A") if verified else colors.HexColor("#F79009")

    story = [
        Paragraph("Expense Report", h1),
        Paragraph(f"Generated {datetime.now():%d %b %Y, %H:%M} · Receipt ID {res.id} · Model {res.model}", sub),
        Spacer(1, 6),
    ]

    # status + summary block
    status = Table(
        [[Paragraph(f"<font color='white'><b>{'VERIFIED' if verified else 'FLAGGED FOR REVIEW'}</b></font>", body),
          Paragraph(f"<b>{res.category}</b><br/><font size=8 color='#667085'>{res.category_reason}</font>", body)]],
        colWidths=[38 * mm, None],
    )
    status.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), badge_color), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
    ]))
    story += [status, Spacer(1, 8)]

    meta = [
        ["Vendor", p.vendor_name or "—", "Date", p.date or "—"],
        ["Address", p.vendor_address or "—", "Invoice #", p.invoice_number or "—"],
        ["Payment", p.payment_method or "—", "Currency", cur],
    ]
    mt = Table([[Paragraph(f"<b>{a}</b>", small), Paragraph(str(b), body), Paragraph(f"<b>{c}</b>", small), Paragraph(str(d), body)] for a, b, c, d in meta],
               colWidths=[20 * mm, 70 * mm, 20 * mm, None])
    mt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(mt)

    # line items
    story.append(Paragraph("Line items", h2))
    rows = [["#", "Description", "Qty", "Rate", "Amount"]]
    for i, li in enumerate(p.line_items, 1):
        rows.append([str(i), Paragraph(li.description, body), "" if li.quantity is None else f"{li.quantity:g}",
                     money(li.unit_price, cur) if li.unit_price is not None else "", money(li.amount, cur)])
    if len(rows) == 1:
        rows.append(["", Paragraph("<i>No itemised rows on this receipt</i>", body), "", "", ""])
    lt = Table(rows, colWidths=[8 * mm, None, 14 * mm, 28 * mm, 30 * mm], repeatRows=1)
    lt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), _FONT_B), ("FONTNAME", (0, 1), (-1, -1), _FONT), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")), ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#D0D5DD")),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FCFCFD")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(lt)

    # totals
    totals = []
    if p.subtotal is not None:
        totals.append(["Subtotal", money(p.subtotal, cur)])
    for t in p.taxes:
        totals.append([f"{t.label}" + (f" ({t.rate_percent:g}%)" if t.rate_percent is not None else ""), money(t.amount, cur)])
    for c in p.other_charges:
        totals.append([c.label, money(c.amount, cur)])
    if p.discount:
        totals.append(["Discount", "- " + money(p.discount, cur)])
    if p.round_off:
        totals.append(["Round off", f"{p.round_off:+.2f}"])
    totals.append(["Total", money(p.total, cur)])
    tt = Table(totals, colWidths=[None, 30 * mm], hAlign="RIGHT")
    tt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -2), _FONT), ("FONTNAME", (0, -1), (-1, -1), _FONT_B), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#101828")),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [Spacer(1, 4), tt]

    # verification
    story.append(Paragraph("Verification", h2))
    vrows = [["", "Check", "Result"]]
    for c in res.verification.checks:
        mark = "PASS" if c.passed else ("FAIL" if c.severity.value == "error" else "WARN")
        vrows.append([mark, Paragraph(c.name, body), Paragraph(c.message, small)])
    vt = Table(vrows, colWidths=[13 * mm, 58 * mm, None])
    vt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), _FONT_B), ("FONTNAME", (0, 1), (0, -1), _FONT_B), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTSIZE", (0, 1), (0, -1), 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    for i, c in enumerate(res.verification.checks, 1):
        vt.setStyle(TableStyle([("TEXTCOLOR", (0, i), (0, i), colors.HexColor("#12B76A") if c.passed else (colors.HexColor("#D92D20") if c.severity.value == "error" else colors.HexColor("#F79009")))]))
    story.append(vt)

    # compliance
    story.append(Paragraph("Compliance", h2))
    pol = res.policy
    pol_color = {"compliant": "#12B76A", "needs_justification": "#F79009", "violation": "#D92D20"}.get(pol.status, "#667085")
    story.append(Paragraph(
        f"<font color='{pol_color}'><b>Policy: {pol.status.replace('_', ' ').upper()}</b></font>"
        + (f" (rules {', '.join(pol.rules_triggered)})" if pol.rules_triggered else "") + f" — {pol.reason}", body))
    dup_flag = res.duplicate.is_duplicate or res.duplicate.similar_image
    story.append(Paragraph(
        ("<font color='#D92D20'><b>Possible duplicate:</b></font> " if dup_flag else "<b>Duplicate check:</b> ")
        + res.duplicate.detail, body))
    if res.claim.claimed_amount is not None or res.claim.claimed_purpose:
        story.append(Paragraph(
            ("<font color='#D92D20'><b>Claim exceeds receipt:</b></font> " if res.claim.status == "exceeds" else "<b>Claim check:</b> ")
            + res.claim.detail + (f" Stated purpose: {res.claim.claimed_purpose}." if res.claim.claimed_purpose else ""), body))

    # agent trace
    story.append(Paragraph("Self-correction trace", h2))
    for e in res.trace:
        line = f"<b>{e.title}</b> — {e.detail}"
        if e.focus_regions:
            line += f" Zoomed into: {', '.join(e.focus_regions)}."
        if e.changes:
            line += "<br/>" + "; ".join(f"{ch.field}: {ch.before} → {ch.after}" for ch in e.changes[:8])
        if e.revision_notes and e.iteration > 0:
            line += f"<br/><i>Agent: {e.revision_notes}</i>"
        story.append(Paragraph(line, small))
        story.append(Spacer(1, 3))
    story.append(Paragraph(
        f"Self-correction {'enabled' if res.self_correction_enabled else 'disabled (baseline single pass)'} · "
        f"{res.iterations} re-examination iteration(s) · {res.total_ms / 1000:.1f}s end to end", small))

    if original_jpeg:
        story.append(Paragraph("Source receipt", h2))
        img = Image(io.BytesIO(original_jpeg))
        ratio = img.imageHeight / float(img.imageWidth)
        w = 70 * mm
        img.drawWidth, img.drawHeight = w, w * ratio
        story.append(img)

    doc.build(story)
    return buf.getvalue()

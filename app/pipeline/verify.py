"""Block 5 - Verification Engine (deterministic logic, no LLM).

Applies arithmetic and completeness constraints to an ExpenseProfile and, for each failure,
produces a hypothesis about the likely cause. The hypotheses feed the self-correction agent's
targeted re-examination prompt.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from .models import Check, ExpenseProfile, Severity, VerificationResult

ABS_TOL = 0.05  # currency rounding noise


def _close(a: Optional[float], b: Optional[float], tol: float = ABS_TOL) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def _fmt(x: Optional[float]) -> str:
    return "?" if x is None else f"{x:,.2f}"


def _decimal_shift_hint(name: str, actual: float, expected: float) -> str:
    """Detect the classic misread decimal: value is ~10x or ~1/10 of what arithmetic needs."""
    if expected == 0:
        return ""
    for k in (10, 100):
        if _close(actual, expected * k, tol=max(0.5, expected * 0.01)):
            return f"{name} ({actual:,.2f}) is about {k}x the arithmetically expected value; a decimal point may have been dropped."
        if _close(actual * k, expected, tol=max(0.5, expected * 0.01)):
            return f"{name} ({actual:,.2f}) is about 1/{k} of the arithmetically expected value; an extra decimal or a missed digit is likely."
    return ""


NUMERIC_FIELDS = ("line_items", "subtotal", "taxes", "discount", "round_off", "total", "other_charges", "amount", "quantity", "unit_price")


def _inclusive_evidence(p: ExpenseProfile, base: float, tax_sum: float) -> str:
    """Only accept the tax-inclusive interpretation when the receipt gives evidence for it:
    the model saw an 'inclusive' statement, or a printed tax rate reproduces the tax amount on the
    net base (total - tax), which is how VAT/GST summaries are printed on inclusive receipts."""
    if p.tax_inclusive_prices is True:
        return "receipt states prices include tax"
    net = base - tax_sum
    if net <= 0:
        return ""
    for t in p.taxes:
        if t.rate_percent and t.amount and _close(net * t.rate_percent / 100, t.amount, tol=max(0.05, t.amount * 0.02)):
            return f"{t.label} {t.rate_percent:g}% of net {net:,.2f} = {t.amount:,.2f}"
    return ""


def verify(p: ExpenseProfile, iteration: int = 0) -> VerificationResult:
    checks: List[Check] = []
    interpretation = ""

    # ---------------------------------------------------------------- C1 completeness
    missing = [f for f in ("vendor_name", "date", "total") if getattr(p, f) in (None, "")]
    checks.append(
        Check(
            id="C1", name="Mandatory fields present",
            passed=not missing,
            severity=Severity.error if "total" in missing else Severity.warning,
            message="All mandatory fields present." if not missing else f"Missing: {', '.join(missing)}.",
            fields=missing,
            hypothesis=(
                "These fields are printed on almost every receipt; look at the header for the "
                "vendor and date and at the bottom for the grand total." if missing else ""
            ),
        )
    )

    items_sum = p.items_sum()
    tax_sum = p.tax_sum()
    charges = p.charges_sum()
    discount = p.discount or 0.0
    round_off = p.round_off or 0.0

    # ---------------------------------------------------------------- C2 items -> subtotal
    if p.subtotal is not None and items_sum is not None:
        ok = _close(items_sum, p.subtotal, tol=ABS_TOL * max(1, len(p.line_items)))
        hyp = ""
        if not ok:
            delta = round(p.subtotal - items_sum, 2)
            hyp = _decimal_shift_hint("subtotal", p.subtotal, items_sum) or (
                f"Line items sum to {_fmt(items_sum)} but subtotal reads {_fmt(p.subtotal)} "
                f"(difference {delta:+,.2f}). A line item may be missing, duplicated, or one "
                f"amount misread. Look for an item priced about {abs(delta):,.2f}."
            )
        checks.append(
            Check(
                id="C2", name="Line items sum to subtotal", passed=ok, severity=Severity.error,
                message=f"Σ items {_fmt(items_sum)} vs subtotal {_fmt(p.subtotal)}.",
                fields=["line_items", "subtotal"], hypothesis=hyp,
                expected=items_sum, actual=p.subtotal,
            )
        )

    # ---------------------------------------------------------------- C3 total equation
    base = p.subtotal if p.subtotal is not None else items_sum
    if p.total is not None and base is not None:
        exclusive = round(base + tax_sum + charges - discount + round_off, 2)
        inclusive = round(base + charges - discount + round_off, 2)  # taxes already inside prices

        # unprinted round-off is only plausible when the total is a whole number
        whole_total = abs(p.total - round(p.total)) < 0.005
        tol = 0.5 if (p.round_off is None and whole_total) else ABS_TOL
        evidence = _inclusive_evidence(p, base, tax_sum) if tax_sum > 0 else ""

        if _close(p.total, exclusive, tol=tol):
            ok, expected = True, exclusive
            interpretation = "tax-exclusive pricing"
        elif tax_sum > 0 and evidence and _close(p.total, inclusive, tol=tol):
            ok, expected = True, inclusive
            interpretation = f"tax-inclusive pricing ({evidence})"
        else:
            ok, expected = False, exclusive

        hyp = ""
        if not ok:
            delta = round(p.total - exclusive, 2)
            parts = [
                f"{'Subtotal' if p.subtotal is not None else 'Σ items'} {_fmt(base)}"
                + (f" + tax {_fmt(tax_sum)}" if tax_sum else "")
                + (f" + charges {_fmt(charges)}" if charges else "")
                + (f" - discount {_fmt(discount)}" if discount else "")
                + (f" {round_off:+,.2f} round-off" if round_off else "")
                + f" = {_fmt(exclusive)}, but total reads {_fmt(p.total)} (difference {delta:+,.2f})."
            ]
            # ranked hypotheses
            for t in p.taxes:
                if t.amount and _close(abs(delta), t.amount, tol=0.5):
                    parts.append(
                        f"The difference equals the {t.label} amount ({t.amount:,.2f}). Indian bills "
                        f"usually print CGST and SGST as two equal rows: a second tax row may have "
                        f"been missed, or one row counted twice."
                    )
                    break
            else:
                d_total = _decimal_shift_hint("total", p.total, exclusive)
                d_tax = _decimal_shift_hint("tax", tax_sum, p.total - base - charges + discount - round_off) if tax_sum else ""
                if d_total:
                    parts.append(d_total)
                elif d_tax:
                    parts.append(d_tax)
                elif tax_sum > 0 and _close(p.total, inclusive, tol=tol):
                    parts.append(
                        f"The total equals the item sum WITHOUT tax, so prices may be tax-inclusive, but the "
                        f"receipt gives no evidence: look for words like 'incl.', 'inclusive of GST/VAT', or a tax "
                        f"summary whose rate reproduces the tax on the net amount, and set tax_inclusive_prices "
                        f"accordingly. Otherwise re-read the total."
                    )
                elif delta > 0:
                    parts.append(
                        f"A charge of about {abs(delta):,.2f} (tax, service charge, delivery, "
                        f"packing, or a line item) may have been missed."
                    )
                else:
                    parts.append(
                        f"About {abs(delta):,.2f} too much was extracted: a discount may have been "
                        f"missed, a tax counted twice, or prices are tax-inclusive."
                    )
            hyp = " ".join(parts)

        checks.append(
            Check(
                id="C3", name="Subtotal + tax + charges - discount = total", passed=ok,
                severity=Severity.error,
                message=f"Expected {_fmt(expected)} vs total {_fmt(p.total)}"
                        + (f" ({interpretation})." if ok and interpretation else "."),
                fields=["total", "taxes", "subtotal", "discount", "other_charges"],
                hypothesis=hyp, expected=expected, actual=p.total,
            )
        )

    # ---------------------------------------------------------------- C4 per-line arithmetic
    bad_lines = []
    for i, li in enumerate(p.line_items):
        if li.quantity is not None and li.unit_price is not None and li.amount is not None:
            if not _close(li.quantity * li.unit_price, li.amount, tol=max(ABS_TOL, li.amount * 0.005)):
                bad_lines.append(f"#{i + 1} {li.description!r}: {li.quantity} x {li.unit_price} ≠ {li.amount}")
    checks.append(
        Check(
            id="C4", name="Quantity x unit price = line amount", passed=not bad_lines,
            severity=Severity.warning,
            message="All line items consistent." if not bad_lines else "; ".join(bad_lines),
            fields=["line_items"],
            hypothesis="Re-read quantity, rate and amount columns for the listed rows." if bad_lines else "",
        )
    )

    # ---------------------------------------------------------------- C5 tax plausibility
    implaus = []
    # for tax-inclusive receipts the taxable base is the total net of tax, not the printed item sum
    rate_base = (base - tax_sum) if (base and "inclusive" in interpretation) else base
    if rate_base and rate_base > 0:
        for t in p.taxes:
            if t.amount is not None:
                rate = 100 * t.amount / rate_base
                if rate < 0 or rate > 30:
                    implaus.append(f"{t.label} = {rate:.1f}% of base")
            if t.rate_percent is not None and t.amount is not None and t.amount > 0:
                if not _close(rate_base * t.rate_percent / 100, t.amount, tol=max(0.5, t.amount * 0.05)):
                    implaus.append(f"{t.label}: {t.rate_percent}% of {_fmt(rate_base)} ≠ {t.amount:,.2f}")
    checks.append(
        Check(
            id="C5", name="Tax amounts plausible", passed=not implaus, severity=Severity.warning,
            message="Tax rates within expected range." if not implaus else "; ".join(implaus),
            fields=["taxes"], hypothesis="Re-read the tax rows; a rate may be confused with an amount." if implaus else "",
        )
    )

    # ---------------------------------------------------------------- C6 date sanity
    date_ok, date_msg, date_sev, date_hyp = True, "Date valid.", Severity.warning, ""
    if p.date:
        try:
            d = datetime.strptime(p.date, "%Y-%m-%d").date()
            if d > date.today():
                date_ok, date_msg = False, f"Date {p.date} is in the future (today is {date.today()})."
                swapped = None
                if d.day <= 12:
                    try:
                        swapped = d.replace(month=d.day, day=d.month)
                    except ValueError:
                        swapped = None
                if swapped and swapped <= date.today():
                    # a swapped day/month yields a plausible past date -> worth a re-examination
                    date_sev = Severity.error
                    date_hyp = (f"A future date cannot be correct. Swapping day and month gives {swapped}, which is "
                                f"plausible: re-read the printed date and decide whether it is DD/MM or MM/DD.")
                else:
                    date_hyp = "Re-read the printed date; a digit may be misread."
            elif d.year < 2000:
                date_ok, date_msg, date_hyp = False, f"Date {p.date} is implausibly old.", "Re-read the year digits."
        except ValueError:
            date_ok, date_msg, date_hyp = False, f"Date {p.date!r} is not YYYY-MM-DD.", "Return the date as YYYY-MM-DD."
    checks.append(
        Check(id="C6", name="Date sane", passed=date_ok, severity=date_sev, message=date_msg,
              fields=["date"], hypothesis=date_hyp)
    )

    # ---------------------------------------------------------------- C8 line-item completeness
    holes = [f"#{i + 1} {li.description!r}" for i, li in enumerate(p.line_items) if li.amount is None]
    checks.append(
        Check(
            id="C8", name="Every line item has an amount", passed=not holes,
            severity=Severity.error if holes and p.line_items else Severity.warning,
            message="All line amounts read." if not holes else f"No amount for {', '.join(holes)}.",
            fields=["line_items"],
            hypothesis=(
                "An unreadable amount usually means the columns were mis-aligned: a price may have been "
                "attached to the wrong row (e.g. a topping/modifier row given the next item's price). Re-read "
                "the item block row by row; modifier rows without their own price get amount 0."
                if holes else ""
            ),
        )
    )

    # ---------------------------------------------------------------- C9 model-declared uncertainty
    # Low confidence on a NUMERIC field earns exactly one targeted re-read (first pass only): a
    # confidently-wrong-but-arithmetically-consistent read is the failure mode sums cannot catch.
    numeric_unc = [f for f in p.uncertain_fields if any(k in f for k in NUMERIC_FIELDS)]
    unc_sev = Severity.error if (numeric_unc and iteration == 0) else Severity.warning
    checks.append(
        Check(
            id="C9", name="Model confident in all numeric fields", passed=not p.uncertain_fields, severity=unc_sev,
            message="No low-confidence fields declared." if not p.uncertain_fields else f"Low confidence: {', '.join(p.uncertain_fields)}.",
            fields=list(p.uncertain_fields),
            hypothesis=(
                "The arithmetic is consistent but you were unsure of these values. Re-read them from the zoomed "
                "crop; in particular make sure each printed price sits on its own row (a topping/modifier row must "
                "not take the next item's price). If the values are right, return them unchanged with an empty "
                "uncertain_fields list." if numeric_unc else ""
            ),
        )
    )

    # ---------------------------------------------------------------- C7 has items
    checks.append(
        Check(
            id="C7", name="At least one line item", passed=bool(p.line_items), severity=Severity.warning,
            message=f"{len(p.line_items)} line item(s)." if p.line_items else "No line items extracted.",
            fields=["line_items"], hypothesis="Itemised rows are usually in the middle band of the receipt." if not p.line_items else "",
        )
    )

    consistent = not any((not c.passed and c.severity == Severity.error) for c in checks)
    return VerificationResult(consistent=consistent, checks=checks, interpretation=interpretation)

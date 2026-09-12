"""Blocks 6/7 - Error Detection & Self-Correction Agent, and the verification-gating state machine.

run_pipeline() orchestrates the whole flow:
  preprocess -> extract -> verify
     -> [flagged? rank failures -> hypothesise -> zoom into suspect regions -> re-examine -> verify]*
     -> gate (VERIFIED / FLAGGED_FOR_REVIEW) -> categorise -> policy check -> duplicate check
Every iteration is recorded as a TraceEntry, so the UI (and the PDF) can show the agent's reasoning.
"""
from __future__ import annotations

import time
import uuid
from typing import Callable, List, Optional

from . import extract as llm
from .models import (
    DuplicateResult, ExpenseProfile, FieldChange, ProcessResult, ReceiptState, TraceEntry,
    VerificationResult,
)
from .preprocess import b64, crop_region, ink_fraction, preprocess
from .verify import verify

ProgressCb = Callable[[str, str], None]  # (stage, message)


def _flat(p: ExpenseProfile) -> dict:
    """Flatten a profile into scalar fields for diffing between iterations."""
    out = {
        "vendor_name": p.vendor_name, "date": p.date, "currency": p.currency,
        "subtotal": p.subtotal, "discount": p.discount, "round_off": p.round_off, "total": p.total,
        "tax_inclusive_prices": p.tax_inclusive_prices,
    }
    for i, li in enumerate(p.line_items):
        out[f"line_items[{i}]"] = f"{li.description} | {li.quantity} x {li.unit_price} = {li.amount}"
    for i, t in enumerate(p.taxes):
        out[f"taxes[{i}]"] = f"{t.label} {t.rate_percent}% = {t.amount}"
    for i, c in enumerate(p.other_charges):
        out[f"other_charges[{i}]"] = f"{c.label} = {c.amount}"
    return out


def diff(a: ExpenseProfile, b: ExpenseProfile) -> List[FieldChange]:
    fa, fb = _flat(a), _flat(b)
    changes = []
    for k in sorted(set(fa) | set(fb)):
        if fa.get(k) != fb.get(k):
            changes.append(FieldChange(field=k, before=None if fa.get(k) is None else str(fa[k]),
                                       after=None if fb.get(k) is None else str(fb[k])))
    return changes


def normalize(p: ExpenseProfile) -> ExpenseProfile:
    """Deterministic clean-up of model output before verification: drop exact duplicate tax /
    charge rows (same label and amount). CGST and SGST differ in label so they survive; a row the
    model emitted twice does not."""
    seen, taxes = set(), []
    for t in p.taxes:
        key = ((t.label or "").strip().lower(), t.rate_percent, t.amount)
        if key not in seen:
            seen.add(key)
            taxes.append(t)
    seen, charges = set(), []
    for c in p.other_charges:
        key = ((c.label or "").strip().lower(), c.amount)
        if key not in seen:
            seen.add(key)
            charges.append(c)
    if len(taxes) != len(p.taxes) or len(charges) != len(p.other_charges):
        p = p.model_copy(update={"taxes": taxes, "other_charges": charges})
    return p


def inject_fault(p: ExpenseProfile) -> tuple[ExpenseProfile, str]:
    """Demo helper: perturb the first extraction so the correction loop is exercised on clean receipts."""
    q = p.model_copy(deep=True)
    if len(q.taxes) >= 2:
        dropped = q.taxes.pop()
        return q, f"dropped tax row '{dropped.label}' ({dropped.amount})"
    if q.total:
        q.total = round(q.total * 10, 2)
        return q, "shifted the decimal point of the total (x10)"
    if q.line_items:
        q.line_items.pop()
        return q, "dropped the last line item"
    return q, "no fault applicable"


FALLBACK_BANDS = {  # where each zone usually sits on a receipt, if the model's box is unusable
    "header": (0.0, 0.0, 1.0, 0.3), "line_items": (0.0, 0.2, 1.0, 0.5),
    "taxes": (0.0, 0.5, 1.0, 0.35), "total": (0.0, 0.55, 1.0, 0.4),
}


def _crops_for(profile: ExpenseProfile, zones: List[str], jpeg: bytes) -> List[tuple[str, bytes]]:
    """Crop the zones the failing checks point at. A model-proposed box is cross-checked with a
    cheap ink-density test; an (almost) blank box means the model's coordinates were off, so we
    fall back to the typical band for that zone instead of zooming into table-top."""
    crops = []
    by_zone = {r.field: r for r in profile.regions}
    for z in zones:
        r = by_zone.get(z)
        try:
            crop = crop_region(jpeg, r.x, r.y, r.w, r.h) if r else None
            if crop is None or ink_fraction(crop) < 0.004:
                x, y, w, h = FALLBACK_BANDS[z]
                crop = crop_region(jpeg, x, y, w, h)
            crops.append((z, crop))
        except Exception:
            continue
    return crops[:3]


def check_duplicate(profile: ExpenseProfile) -> DuplicateResult:
    """Deterministic duplicate-submission check against the ledger."""
    try:
        from . import storage

        hit = storage.find_similar(profile.vendor_name, profile.date, profile.total)
    except Exception:
        hit = None
    if hit:
        return DuplicateResult(is_duplicate=True, matched_id=hit["id"],
                               detail=f"Same vendor, date and total as receipt {hit['id']} submitted {hit['created_at'][:16]}.")
    return DuplicateResult(is_duplicate=False, detail="No matching receipt in the ledger.")


def run_pipeline(
    image_bytes: bytes,
    *,
    self_correct: bool = True,
    max_iterations: int = 3,
    demo_fault: bool = False,
    policy_text: Optional[str] = None,
    submitted_by: Optional[str] = None,
    department: Optional[str] = None,
    progress: Optional[ProgressCb] = None,
) -> ProcessResult:
    t0 = time.time()
    rid = uuid.uuid4().hex[:12]
    trace: List[TraceEntry] = []

    def say(stage: str, msg: str):
        if progress:
            progress(stage, msg)

    # ---------------------------------------------------------------- 1-2 ingest + preprocess
    say("preprocess", "Enhancing image (orient, denoise, deskew, contrast)")
    pre = preprocess(image_bytes)

    # ---------------------------------------------------------------- 3-4 extraction
    say("extract", f"Building initial expense profile with {llm.MODEL}")
    ts = time.time()
    profile = normalize(llm.extract(pre.enhanced_jpeg))
    fault_note = ""
    if demo_fault:
        profile, fault_note = inject_fault(profile)
    trace.append(TraceEntry(
        iteration=0, state=ReceiptState.initial_extraction, title="Initial extraction",
        detail=f"{len(profile.line_items)} line items, {len(profile.taxes)} tax rows, total {profile.total}"
               + (f". Demo fault injected: {fault_note}." if demo_fault else "."),
        duration_ms=int((time.time() - ts) * 1000), demo_fault=demo_fault,
        revision_notes=profile.notes,
    ))

    # ---------------------------------------------------------------- 5 verification
    say("verify", "Running deterministic arithmetic & logic checks")
    result: VerificationResult = verify(profile)
    trace.append(_verify_entry(0, result))

    # ---------------------------------------------------------------- 6 self-correction loop
    iterations = 0
    if self_correct:
        while not result.consistent and iterations < max_iterations:
            iterations += 1
            failed = sorted(result.failed_errors(), key=lambda c: c.id)  # C1 completeness, then C2, C3
            zones = llm.zones_for(failed)
            crops = _crops_for(profile, zones, pre.enhanced_jpeg)
            prompt = llm.build_reexamination_prompt(profile, failed, n_crops=len(crops))
            say("correct", f"Iteration {iterations}: zooming into {', '.join(zones) or 'receipt'} and re-examining")
            ts = time.time()
            revised = normalize(llm.reexamine(pre.enhanced_jpeg, profile, prompt, iterations, crops=crops))
            changes = diff(profile, revised)
            trace.append(TraceEntry(
                iteration=iterations, state=ReceiptState.flagged_reexamination,
                title=f"Targeted re-examination #{iterations}",
                detail=f"{len(failed)} failing constraint(s) → hypothesis → zoomed into {len(crops)} region(s) → "
                       f"{len(changes)} field(s) changed.",
                prompt=prompt, failed_checks=[f"{c.id} {c.name}" for c in failed], changes=changes,
                revision_notes=revised.revision_notes, duration_ms=int((time.time() - ts) * 1000),
                focus_regions=[z for z, _ in crops], crops_b64=[b64(c) for _, c in crops],
            ))
            if not revised.regions:
                revised.regions = profile.regions
            profile = revised
            say("verify", f"Re-verifying after iteration {iterations}")
            result = verify(profile, iteration=iterations)
            trace.append(_verify_entry(iterations, result))
            if not changes and not result.consistent:
                trace[-1].detail += " Model returned identical values; stopping early."
                break

    # ---------------------------------------------------------------- 7 gate
    state = ReceiptState.verified if result.consistent else ReceiptState.flagged_for_review
    trace.append(TraceEntry(
        iteration=iterations, state=state,
        title="Verified — profile locked" if result.consistent else "Flagged for human review",
        detail=("All arithmetic constraints satisfied" + (f" ({result.interpretation})" if result.interpretation else "") + ".")
        if result.consistent else
        f"{len(result.failed_errors())} constraint(s) still failing after {iterations} re-examination(s); "
        f"data is kept but marked, never silently accepted.",
    ))

    # ---------------------------------------------------------------- 8 categorisation + compliance
    say("categorize", "Classifying expense category")
    category, reason = llm.categorize(profile)
    say("policy", "Checking against expense policy")
    policy = llm.check_policy(profile, category, policy_text or llm.DEFAULT_POLICY)
    duplicate = check_duplicate(profile)

    return ProcessResult(
        id=rid, state=state, profile=profile, verification=result, trace=trace,
        category=category, category_reason=reason, policy=policy, duplicate=duplicate,
        iterations=iterations, max_iterations=max_iterations, self_correction_enabled=self_correct,
        submitted_by=submitted_by or None, department=department or None,
        model=("mock" if llm.MOCK else llm.MODEL),
        original_image_b64=b64(pre.original_jpeg), processed_image_b64=b64(pre.binary_jpeg),
        preprocess_info=pre.info, total_ms=int((time.time() - t0) * 1000),
    )


def _verify_entry(iteration: int, result: VerificationResult) -> TraceEntry:
    errs, warns = result.failed_errors(), result.failed_warnings()
    if result.consistent:
        detail = f"All error-level checks pass. {len(warns)} warning(s)."
    else:
        detail = "Failing: " + "; ".join(f"{c.id} {c.message}" for c in errs)
    return TraceEntry(
        iteration=iteration, state=ReceiptState.awaiting_verification,
        title="Verification" + (f" (after iteration {iteration})" if iteration else ""),
        detail=detail, failed_checks=[f"{c.id} {c.name}" for c in errs + warns],
    )

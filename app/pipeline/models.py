"""Knowledge representation for the pipeline.

Three structures track a receipt end to end (matches section V.1 of the proposal):
  * ExpenseProfile      - the structured extraction (vendor, date, line items, taxes, total ...)
  * VerificationResult  - arithmetic / logic consistency status and discrepancy flags
  * TraceEntry          - one agentic decision-trace entry per self-correction iteration
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- profile
class LineItem(BaseModel):
    description: str = ""
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


class TaxLine(BaseModel):
    label: str = "Tax"
    rate_percent: Optional[float] = None
    amount: Optional[float] = None


class ChargeLine(BaseModel):
    label: str = ""
    amount: Optional[float] = None


class Region(BaseModel):
    """Approximate location of a field on the image, normalised 0-1 (x, y = top-left)."""
    field: str
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 0.1


class ExpenseProfile(BaseModel):
    vendor_name: Optional[str] = None
    vendor_address: Optional[str] = None
    invoice_number: Optional[str] = None
    date: Optional[str] = None          # YYYY-MM-DD when known
    time: Optional[str] = None
    currency: str = "INR"
    line_items: List[LineItem] = Field(default_factory=list)
    subtotal: Optional[float] = None
    taxes: List[TaxLine] = Field(default_factory=list)
    other_charges: List[ChargeLine] = Field(default_factory=list)
    discount: Optional[float] = None
    round_off: Optional[float] = None
    total: Optional[float] = None
    payment_method: Optional[str] = None
    tax_inclusive_prices: Optional[bool] = None
    uncertain_fields: List[str] = Field(default_factory=list)
    regions: List[Region] = Field(default_factory=list)
    notes: str = ""
    revision_notes: str = ""

    @field_validator("vendor_name", "vendor_address", "invoice_number", "date", "time", "payment_method", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # convenience -----------------------------------------------------------
    def items_sum(self) -> Optional[float]:
        amounts = [li.amount for li in self.line_items if li.amount is not None]
        return round(sum(amounts), 2) if amounts else None

    def tax_sum(self) -> float:
        return round(sum(t.amount or 0.0 for t in self.taxes), 2)

    def charges_sum(self) -> float:
        return round(sum(c.amount or 0.0 for c in self.other_charges), 2)


# JSON schema handed to the model for structured output. Kept in sync with ExpenseProfile
# by hand so we control nullability and descriptions precisely.
def _num(desc: str) -> dict:
    return {"type": ["number", "null"], "description": desc}


def _str(desc: str) -> dict:
    return {"type": ["string", "null"], "description": desc}


def _txt(desc: str) -> dict:
    # plain string (empty when absent) - the API caps nullable/union fields at 16 per schema
    return {"type": "string", "description": desc + " Empty string if not printed."}


EXPENSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "vendor_name": _str("Merchant / store / restaurant name as printed."),
        "vendor_address": _txt("Address or city if printed."),
        "invoice_number": _txt("Bill / invoice / receipt number if printed."),
        "date": _str("Purchase date in YYYY-MM-DD. Indian receipts are usually DD/MM/YYYY."),
        "time": _txt("Time of purchase if printed, HH:MM."),
        "currency": {"type": "string", "description": "ISO code, e.g. INR, USD."},
        "line_items": {
            "type": "array",
            "description": "Every purchased item row. Exclude subtotal/tax/total rows.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "description": {"type": "string"},
                    "quantity": _num("Quantity, null if not printed."),
                    "unit_price": _num("Price per unit, null if not printed."),
                    "amount": _num("Row amount (quantity x unit price) as printed."),
                },
                "required": ["description", "quantity", "unit_price", "amount"],
            },
        },
        "subtotal": _num("Subtotal before tax, null if not printed."),
        "taxes": {
            "type": "array",
            "description": "One entry per tax row printed (CGST, SGST, IGST, VAT, service tax ...).",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "rate_percent": _num("Rate, e.g. 2.5 for 2.5%."),
                    "amount": _num("Tax amount."),
                },
                "required": ["label", "rate_percent", "amount"],
            },
        },
        "other_charges": {
            "type": "array",
            "description": "Service charge, delivery fee, packing charge etc.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"label": {"type": "string"}, "amount": _num("Charge amount.")},
                "required": ["label", "amount"],
            },
        },
        "discount": _num("Total discount as a positive number, null if none."),
        "round_off": _num("Round-off adjustment, signed, null if none."),
        "total": _num("Grand total actually payable."),
        "payment_method": _txt("Cash, card, UPI ... if printed."),
        "tax_inclusive_prices": {
            "type": ["boolean", "null"],
            "description": "True if item prices already include tax (tax rows are informational).",
        },
        "uncertain_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of fields you could not read confidently.",
        },
        "regions": {
            "type": "array",
            "description": (
                "Approximate bounding boxes, normalised 0-1 relative to the full image, for these "
                "zones: 'header' (vendor/date), 'line_items' (the itemised block), 'taxes' (tax rows), "
                "'total' (grand total row). x,y is the top-left corner; w,h the size."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string", "enum": ["header", "line_items", "taxes", "total"]},
                    "x": {"type": "number"}, "y": {"type": "number"},
                    "w": {"type": "number"}, "h": {"type": "number"},
                },
                "required": ["field", "x", "y", "w", "h"],
            },
        },
        "notes": {"type": "string", "description": "Short remarks about legibility or layout."},
        "revision_notes": {
            "type": "string",
            "description": "On a re-examination pass: what you changed and why. Empty on first pass.",
        },
    },
    "required": [
        "vendor_name", "vendor_address", "invoice_number", "date", "time", "currency",
        "line_items", "subtotal", "taxes", "other_charges", "discount", "round_off",
        "total", "payment_method", "tax_inclusive_prices", "uncertain_fields", "regions", "notes",
        "revision_notes",
    ],
}


# ---------------------------------------------------------------- verification
class Severity(str, Enum):
    error = "error"      # blocks verification, triggers re-examination
    warning = "warning"  # reported, does not block


class Check(BaseModel):
    id: str
    name: str
    passed: bool
    severity: Severity
    message: str
    fields: List[str] = Field(default_factory=list)   # fields to re-examine
    hypothesis: str = ""                              # why it might have failed
    expected: Optional[float] = None
    actual: Optional[float] = None


class VerificationResult(BaseModel):
    consistent: bool                       # no failed error-severity checks
    checks: List[Check]
    interpretation: str = ""               # e.g. "tax-inclusive pricing"

    def failed_errors(self) -> List[Check]:
        return [c for c in self.checks if not c.passed and c.severity == Severity.error]

    def failed_warnings(self) -> List[Check]:
        return [c for c in self.checks if not c.passed and c.severity == Severity.warning]


# -------------------------------------------------------------------- state / trace
class ReceiptState(str, Enum):
    initial_extraction = "INITIAL_EXTRACTION"
    awaiting_verification = "AWAITING_VERIFICATION"
    flagged_reexamination = "FLAGGED_FOR_REEXAMINATION"
    verified = "VERIFIED"
    flagged_for_review = "FLAGGED_FOR_REVIEW"


class FieldChange(BaseModel):
    field: str
    before: Optional[str] = None
    after: Optional[str] = None


class TraceEntry(BaseModel):
    iteration: int                          # 0 = initial extraction
    state: ReceiptState
    title: str
    detail: str = ""
    prompt: str = ""                        # targeted re-examination prompt sent (if any)
    failed_checks: List[str] = Field(default_factory=list)
    changes: List[FieldChange] = Field(default_factory=list)
    revision_notes: str = ""
    duration_ms: int = 0
    demo_fault: bool = False
    focus_regions: List[str] = Field(default_factory=list)   # zones the agent zoomed into
    crops_b64: List[str] = Field(default_factory=list)       # the actual crops sent to the model


class PolicyResult(BaseModel):
    status: str = "not_checked"     # compliant | needs_justification | violation | not_checked
    reason: str = ""
    rules_triggered: List[str] = Field(default_factory=list)


class DuplicateResult(BaseModel):
    is_duplicate: bool = False
    matched_id: Optional[str] = None
    detail: str = ""


class ProcessResult(BaseModel):
    id: str
    state: ReceiptState
    profile: ExpenseProfile
    verification: VerificationResult
    trace: List[TraceEntry]
    category: str = "Uncategorized"
    category_reason: str = ""
    policy: PolicyResult = Field(default_factory=PolicyResult)
    duplicate: DuplicateResult = Field(default_factory=DuplicateResult)
    iterations: int = 0
    max_iterations: int = 3
    self_correction_enabled: bool = True
    submitted_by: Optional[str] = None      # employee name from the organisation directory
    department: Optional[str] = None
    model: str = ""
    original_image_b64: str = ""
    processed_image_b64: str = ""
    preprocess_info: dict = Field(default_factory=dict)
    pdf_path: Optional[str] = None
    total_ms: int = 0

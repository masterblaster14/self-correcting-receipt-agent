"""Blocks 3/4 and 8 - Multimodal LLM extraction, attention-focused re-examination,
categorisation, policy compliance reasoning, and the natural-language ledger agent.

Uses Claude vision through the official Anthropic SDK with JSON-schema structured output,
so every response is guaranteed to parse into our pydantic models.

Set MOCK_LLM=1 to run the whole pipeline offline with canned responses (useful for UI work
and for testing the correction loop without spending tokens).
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import List, Optional

from .models import EXPENSE_SCHEMA, ExpenseProfile, PolicyResult

MODEL = os.environ.get("MODEL", "claude-opus-5")
EFFORT = os.environ.get("EFFORT", "medium")       # low | medium | high  (latency vs. care)
MOCK = os.environ.get("MOCK_LLM", "0") == "1"

CATEGORIES = [
    "Meals & Dining", "Travel & Transport", "Accommodation", "Office Supplies", "Groceries",
    "Fuel", "Utilities & Telecom", "Software & Subscriptions", "Medical", "Entertainment",
    "Shopping & Apparel", "Other",
]

SYSTEM_EXTRACT = """You are the perception module of an expense-report agent.
You read photographs of retail receipts and invoices (often Indian: GST with CGST/SGST rows,
amounts in INR, dates as DD/MM/YYYY) and return a structured expense profile.

Rules:
- Transcribe numbers exactly as printed. Never invent a value; use null when unreadable and
  list that field in uncertain_fields.
- Every purchased item row goes in line_items. Do NOT put subtotal, tax, discount, total rows there.
- Read the item block row by row, keeping each printed price on its own row. A topping / modifier /
  free row that has no price of its own gets amount 0 (not null). Use null only when a printed
  amount is genuinely unreadable.
- If an item name is not in English (Hindi, Tamil, Kannada, ...), write the English meaning
  followed by the original in parentheses.
- Every tax row printed becomes its own entry in taxes (CGST and SGST are two entries).
- total is the final amount payable (Grand Total / Net Amount / Amount Payable).
- Convert dates to YYYY-MM-DD.
- Set tax_inclusive_prices true only if the receipt says prices include tax / MRP inclusive.
- Fill regions with your best estimate of where the header, line_items block, taxes block and
  total row sit on the image (normalised 0-1). They guide a later zoom-in, so be generous.
- Do not "fix" arithmetic yourself: report what is printed. A separate verifier checks the math."""

FIRST_PASS_PROMPT = "Extract the structured expense profile from this receipt image."

REEXAMINE_TEMPLATE = """This is a RE-EXAMINATION pass. Your previous extraction of the same receipt
failed deterministic verification. Previous extraction (JSON):

{previous}

Failed checks and hypotheses, most severe first:
{issues}

Focus your attention on these fields: {fields}.
{crop_note}
Look again at the exact regions where these values are printed, especially at digits that are
easily confused (1/7, 3/8, 5/6, 0/8), decimal points, and rows you may have skipped (a second
tax line, a service charge, a discount, a round-off).

Return the FULL corrected expense profile. Keep every field you still believe is right.
If, after looking again, the receipt itself is printed inconsistently, keep the values exactly
as printed and explain that in notes. Describe what you changed and why in revision_notes."""

SYSTEM_CATEGORY = "You classify verified expense records into accounting categories. Reply with JSON only."

CATEGORY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "reason": {"type": "string", "description": "One short sentence."},
    },
    "required": ["category", "reason"],
}

DEFAULT_POLICY = """Company expense policy
1. Meals: up to INR 800 per person per meal. Above that needs a written justification.
2. Alcohol is never reimbursable.
3. Groceries and personal shopping are not reimbursable unless marked as office pantry supplies.
4. Fuel and travel are reimbursable with a receipt; taxi rides above INR 2,000 need justification.
5. Any single receipt above INR 10,000 needs manager pre-approval.
6. Receipts older than 30 days at submission time are not reimbursable.
7. Medical expenses are covered only under the health plan, not as expenses."""

SYSTEM_POLICY = """You are the compliance reviewer of an expense-report agent. Given the company
policy and one verified expense record, decide: compliant, needs_justification, or violation.
Quote the rule numbers you relied on. Be strict but fair; when the record lacks the information
to decide (e.g. number of diners), say needs_justification and explain what is missing."""

POLICY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["compliant", "needs_justification", "violation"]},
        "reason": {"type": "string", "description": "One or two sentences for the employee."},
        "rules_triggered": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['1', '5']"},
    },
    "required": ["status", "reason", "rules_triggered"],
}

_client = None


def client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


def _image_block(jpeg: bytes) -> dict:
    import base64

    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpeg).decode("ascii")}}


def _call_json(system: str, content: list, schema: dict, max_tokens: int = 8000, effort: str = EFFORT) -> dict:
    resp = client().messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Model refused the request.")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# ------------------------------------------------------------------ extraction
def extract(jpeg: bytes) -> ExpenseProfile:
    """First-pass extraction from the enhanced receipt image."""
    if MOCK:
        return _mock_first_pass()
    data = _call_json(SYSTEM_EXTRACT, [_image_block(jpeg), {"type": "text", "text": FIRST_PASS_PROMPT}], EXPENSE_SCHEMA)
    return ExpenseProfile.model_validate(data)


FIELD_TO_ZONE = {
    "vendor_name": "header", "date": "header", "line_items": "line_items", "subtotal": "taxes",
    "taxes": "taxes", "discount": "taxes", "other_charges": "taxes", "round_off": "taxes", "total": "total",
}


def zones_for(failed_checks) -> List[str]:
    """Map failing fields (incl. free-text ones like 'line_items[2].amount') to image zones."""
    zones: List[str] = []
    for c in failed_checks:
        for f in c.fields:
            root = f.split("[")[0].split(".")[0].strip()
            z = FIELD_TO_ZONE.get(root)
            if z and z not in zones:
                zones.append(z)
    return zones


def build_reexamination_prompt(previous: ExpenseProfile, failed_checks, n_crops: int = 0) -> str:
    issues = "\n".join(f"- [{c.id}] {c.name}: {c.message} {c.hypothesis}".strip() for c in failed_checks)
    fields = sorted({f for c in failed_checks for f in c.fields})
    crop_note = (
        f"After the full image, {n_crops} zoomed-in crop(s) of the suspect regions are attached; "
        f"read the digits from the crops, use the full image for context."
        if n_crops else ""
    )
    return REEXAMINE_TEMPLATE.format(
        previous=json.dumps(previous.model_dump(exclude={"revision_notes", "regions"}), indent=1, ensure_ascii=False),
        issues=issues, fields=", ".join(fields) or "all numeric fields", crop_note=crop_note,
    )


def reexamine(jpeg: bytes, previous: ExpenseProfile, prompt: str, iteration: int,
              crops: Optional[List[tuple[str, bytes]]] = None) -> ExpenseProfile:
    """Targeted re-examination: full image + zoomed crops + previous JSON + discrepancy prompt."""
    if MOCK:
        return _mock_reexamine(previous, iteration)
    content: list = [_image_block(jpeg)]
    for zone, crop in crops or []:
        content.append({"type": "text", "text": f"Zoomed crop of the '{zone}' region:"})
        content.append(_image_block(crop))
    content.append({"type": "text", "text": prompt})
    data = _call_json(SYSTEM_EXTRACT, content, EXPENSE_SCHEMA)
    return ExpenseProfile.model_validate(data)


# ------------------------------------------------------------------ categorisation
def categorize(profile: ExpenseProfile) -> tuple[str, str]:
    if MOCK:
        return ("Meals & Dining", "Restaurant vendor with food items (mock).")
    summary = {"vendor_name": profile.vendor_name, "vendor_address": profile.vendor_address,
               "items": [li.description for li in profile.line_items][:25], "total": profile.total, "currency": profile.currency}
    try:
        data = _call_json(SYSTEM_CATEGORY,
                          [{"type": "text", "text": f"Categories: {CATEGORIES}\n\nExpense: {json.dumps(summary, ensure_ascii=False)}"}],
                          CATEGORY_SCHEMA, max_tokens=300, effort="low")
        return data["category"], data["reason"]
    except Exception as e:  # categorisation must never sink a verified receipt
        return "Other", f"Categorisation unavailable ({type(e).__name__})."


# ------------------------------------------------------------------ policy compliance
def check_policy(profile: ExpenseProfile, category: str, policy_text: str) -> PolicyResult:
    if MOCK:
        return PolicyResult(status="compliant", reason="Meal of INR 441.50 is within the per-meal limit (mock).", rules_triggered=["1"])
    record = profile.model_dump(include={"vendor_name", "date", "currency", "line_items", "total", "payment_method"})
    record["category"] = category
    try:
        data = _call_json(SYSTEM_POLICY,
                          [{"type": "text", "text": f"POLICY:\n{policy_text}\n\nEXPENSE RECORD:\n{json.dumps(record, ensure_ascii=False)}\n\nToday's date: {__import__('datetime').date.today().isoformat()}"}],
                          POLICY_SCHEMA, max_tokens=600, effort="low")
        return PolicyResult.model_validate(data)
    except Exception as e:
        return PolicyResult(status="not_checked", reason=f"Policy check unavailable ({type(e).__name__}).")


# ------------------------------------------------------------------ ledger agent (tool use)
LEDGER_TOOL = {
    "name": "query_ledger",
    "description": "Run a read-only SQLite SELECT over the expense ledger and get rows back as JSON.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
        "required": ["sql"],
    },
}

SYSTEM_LEDGER = """You are the analyst of an expense-report agent. Answer questions about the user's
expense ledger by querying SQLite with the query_ledger tool, then reply in plain language with the
numbers. Amounts are in INR unless currency says otherwise. Today is {today}.

Table receipts(id TEXT, created_at TEXT ISO, vendor TEXT, date TEXT 'YYYY-MM-DD', total REAL,
currency TEXT, category TEXT, state TEXT 'VERIFIED'|'FLAGGED_FOR_REVIEW', iterations INTEGER,
self_correct INTEGER, model TEXT, result_json TEXT).
Column meanings that are easy to confuse:
- iterations = number of self-correction passes the agent actually ran. 0 means the first
  extraction already passed verification ("needed no correction"). >0 means it was corrected.
- self_correct = 1 if the self-correction loop was merely ENABLED for that run, 0 if the run was a
  single-pass baseline. It does NOT mean a correction happened; use iterations for that.
- state = FLAGGED_FOR_REVIEW means arithmetic never became consistent (human review needed).
result_json holds the full record; json_extract(result_json, '$.profile.line_items') lists items,
'$.policy.status' the compliance status, '$.duplicate.is_duplicate' the duplicate flag.
Categories: {categories}. Use LIKE for fuzzy vendor matches. Do not mix currencies in one sum.
Answer in plain text (no markdown symbols), short and concrete."""


def ask_ledger(question: str, db_path: str) -> dict:
    """Small tool-using agent: Claude writes SQL, we execute it read-only, Claude answers."""
    if MOCK:
        return {"answer": "Mock mode: connect an API key to query the ledger in natural language.", "queries": []}
    import datetime

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    queries: List[dict] = []

    def run_sql(sql: str) -> str:
        s = sql.strip().rstrip(";")
        if not s.lower().startswith("select") and not s.lower().startswith("with"):
            return json.dumps({"error": "Only SELECT statements are allowed."})
        try:
            rows = [dict(r) for r in conn.execute(s).fetchmany(200)]
            queries.append({"sql": s, "rows": len(rows)})
            return json.dumps(rows, ensure_ascii=False, default=str)[:20000]
        except sqlite3.Error as e:
            queries.append({"sql": s, "error": str(e)})
            return json.dumps({"error": str(e)})

    messages = [{"role": "user", "content": question}]
    system = SYSTEM_LEDGER.format(today=datetime.date.today().isoformat(), categories=CATEGORIES)
    for _ in range(6):
        resp = client().messages.create(model=MODEL, max_tokens=4000, system=system, messages=messages,
                                        tools=[LEDGER_TOOL], output_config={"effort": "low"})
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            answer = "".join(b.text for b in resp.content if b.type == "text")
            return {"answer": answer.strip(), "queries": queries}
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": run_sql(b.input["sql"])})
        messages.append({"role": "user", "content": results})
    return {"answer": "I could not finish answering within the query budget.", "queries": queries}


# ------------------------------------------------------------------ mock responses
def _mock_first_pass() -> ExpenseProfile:
    # deliberately misses the SGST row so the verifier fails and the loop has work to do
    return ExpenseProfile(
        vendor_name="Sri Krishna Cafe", vendor_address="Koramangala, Bengaluru", invoice_number="B-10422",
        date="2026-09-10", time="13:42", currency="INR",
        line_items=[
            {"description": "Masala Dosa", "quantity": 2, "unit_price": 120, "amount": 240},
            {"description": "Filter Coffee", "quantity": 2, "unit_price": 40, "amount": 80},
            {"description": "Idli Vada", "quantity": 1, "unit_price": 90, "amount": 90},
        ],
        subtotal=410, taxes=[{"label": "CGST", "rate_percent": 2.5, "amount": 10.25}],
        other_charges=[], discount=None, round_off=0.5, total=431, payment_method="UPI",
        tax_inclusive_prices=False, uncertain_fields=["taxes"],
        regions=[{"field": "header", "x": 0.05, "y": 0.02, "w": 0.9, "h": 0.2},
                 {"field": "line_items", "x": 0.05, "y": 0.3, "w": 0.9, "h": 0.25},
                 {"field": "taxes", "x": 0.05, "y": 0.55, "w": 0.9, "h": 0.2},
                 {"field": "total", "x": 0.05, "y": 0.75, "w": 0.9, "h": 0.1}],
        notes="Thermal print, slightly faded tax block.",
    )


def _mock_reexamine(previous: ExpenseProfile, iteration: int) -> ExpenseProfile:
    from .models import TaxLine

    fixed = previous.model_copy(deep=True)
    if len(fixed.taxes) == 1:
        fixed.taxes.append(TaxLine(label="SGST", rate_percent=2.5, amount=10.25))
        fixed.revision_notes = "Found a second tax row (SGST 2.5% = 10.25) directly under CGST that was missed in the faded tax block."
    fixed.uncertain_fields = []
    if fixed.total and fixed.subtotal and fixed.total > fixed.subtotal * 5:
        fixed.total = round(fixed.total / 10, 2)
        fixed.revision_notes += " Re-read the total: the decimal point was missed."
    return ExpenseProfile.model_validate(fixed.model_dump())

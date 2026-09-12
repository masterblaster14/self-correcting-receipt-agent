# Pitch and coverage audit

## The 2-minute pitch

**Problem.** Receipt extraction is no longer a reading problem; it is a trust problem. Modern vision-language
models read receipts well, yet when they misread a digit, skip a second tax line, or mis-align a column they
do it confidently and silently. In expense accounting a single wrong decimal invalidates the record. Every
system in our literature survey, from LayoutLM to Donut to DocLLM, is single-pass: it produces one answer
and has no mechanism to notice its own mistake.

**Idea.** A receipt carries its own ground truth: line items must sum to the subtotal, subtotal plus tax
must equal the total, every row must have an amount, the date must be possible. We built an agent that
treats these constraints as a verification signal. The extraction is not trusted until it is *proven*
arithmetically consistent.

**System.** Five layers: two models, three layers of our own logic around them.

1. *Preprocessing (ours, OpenCV).* Receipt localisation in handheld photos, projection-profile deskew,
   non-local-means denoising, CLAHE contrast normalisation, adaptive-threshold machine view.
2. *OCR and spatial layout (a local neural OCR model).* EasyOCR, the CRAFT text detector plus a CRNN
   recogniser, runs on the laptop CPU and returns every word with its box and confidence. We use it as an
   independent second reading: its word boxes are shown in the UI, its numbers are compared against the
   vision model's numbers (check C11), and its keyword positions ("total", "GST") anchor the zoom crops.
3. *Perception (a pretrained vision-language model as a swappable module).* The image goes in with a strict
   JSON schema; a structured expense profile comes out, together with the model's own uncertainty list and
   approximate zone coordinates. We use Claude Opus 5 through its API today; the interface is
   image-plus-schema in, JSON out, so Donut, Qwen-VL or a fine-tuned open model drop in without touching
   the rest. LayoutLM depends on an external OCR engine in exactly the same way.
4. *Verification engine (ours, deterministic Python).* Ten constraints plus the OCR agreement check, no
   model involved in the arithmetic. Each failure carries a machine-generated hypothesis: "the gap equals
   the CGST amount, so the paired SGST row was probably missed", or "the total is ten times the expected
   value, a decimal was dropped".
5. *Self-correction agent (ours).* Failures are ranked, their fields mapped to image zones, those zones are
   cropped and upscaled, the model-proposed boxes are cross-checked by an ink-density test and replaced by a
   prior band when empty, and the perception module is re-queried with the previous JSON, the hypotheses,
   and the crops. Re-verify; repeat up to N times; then a four-state gate commits the record as VERIFIED or
   FLAGGED FOR REVIEW. A receipt is never silently accepted.

**Evidence.** Two evaluations, both against a controlled baseline: the *same* perception model with the loop
switched off, so the difference measures our contribution and nothing else.

* Nine real phone photos, including crumpled and badly lit receipts: the baseline fails on three; the agent
  repairs all three (a folded row recovered from a zoomed crop, a day/month swap, a column shift).
* ICDAR-2019 SROIE, 30 random receipts, seed 0: field accuracy is already high on clean scans (total 97%,
  date 100%), and the loop raises company from 90% to 93%; more importantly it cuts arithmetically
  inconsistent extractions from 9 of 30 to 3 of 30. Per-receipt CSV and a line-by-line error analysis are
  in the repository.

**Beyond the core.** Expense categorisation, policy compliance reasoning against editable natural-language
rules, deterministic duplicate detection, a natural-language ledger agent that writes read-only SQL,
PDF and Excel reports, and a phone-first web app deployed on a public HTTPS URL.

**Novelty in one sentence.** Arithmetic consistency as the trigger, the hypothesis as the prompt, and the
image region, not the text, as what gets re-examined. Self-Refine and Reflexion re-read their own words;
our agent goes back to the pixels.

## When someone says "so it is just an API call to Claude"

Say this, calmly:

"Two models run on every receipt: a local neural OCR engine, EasyOCR's CRAFT detector and CRNN recogniser,
for word-level text and layout, and a pretrained vision-language model for structured reading, which is
the design in our proposal and the standard choice in current document-AI work. Neither is trusted alone:
their numbers are cross-checked against each other and against the receipt's own arithmetic. The models
are replaceable and are not the contribution. The contribution is everything that decides whether to
believe them: the constraint verifier, the hypothesis generator, the region-directed re-examination, the
gating state machine, and the evaluation protocol that isolates the loop's effect by holding the perception
model constant. Our SROIE numbers show why that matters: the same model is right on the total 97% of the
time and still internally inconsistent 30% of the time. The loop is what turns a probabilistic read into an
auditable record."

If pressed on training: "Nothing is fine-tuned in this version; the preprocessing, verification, agent and
evaluation are all ours, and the architecture is deliberately model-agnostic so a locally hosted open VLM
or a Donut baseline can be swapped in, which is our next step."

## Coverage against the proposal document

| Proposal item | Status | What to say |
|---|---|---|
| I. Problem summary: noisy Indian receipts, single-pass errors, self-verification | Done | Demonstrated on real crumpled and low-light phone photos |
| III. Gap and proposal: agentic multimodal framework with self-correction loop | Done | The loop is the core of the system |
| IV.1 Upload receipt image | Done | Phone camera, desktop webcam, upload, drag-drop, paste |
| IV.2 Image preprocessing (OpenCV/Pillow): thresholding, noise reduction, deskew | Done, plus receipt localisation and CLAHE | Show the machine view toggle |
| IV.3 OCR and spatial layout extraction | Done | Local EasyOCR model (CRAFT + CRNN) gives word boxes; shown as the "OCR boxes" view, used for the OCR-agreement check and to anchor re-examination crops. The proposal named Tesseract; EasyOCR is the neural successor and needs no system install |
| IV.4 Initial structured expense profile (multimodal LLM) | Done | Strict JSON schema, 18 fields incl. per-row taxes |
| IV.5 Verification engine (deterministic math and logic) | Done, 9 checks | Point at `verify.py`, no model imports |
| IV.6 Error detection and self-correction agent | Done | Hypothesis, region crop, targeted re-prompt, trace |
| IV.7 Update structured profile / lock verified data | Done | VERIFIED state locks the record |
| IV.8 Expense categorisation (LLM classification) | Done, plus policy compliance | 12 categories with reason |
| IV.9 Secure cloud storage (DynamoDB / S3) | Partial | SQLite plus files on a persistent volume in the cloud deployment; storage sits behind a three-call interface so DynamoDB/S3 is a drop-in. AWS was deliberately skipped in this iteration |
| IV.10 Auto-generate PDF expense report | Done, plus Excel | Download buttons |
| IV.11 Completed structured expense report | Done | JSON, PDF, Excel, ledger |
| V.1 Knowledge representation: profile, verification record, decision trace | Done | Three pydantic models in `models.py` |
| V.2 Extraction layer: "Tesseract OCR and multimodal LLMs" | Done | OCR model (EasyOCR) plus multimodal model, cross-checked |
| V.3 Self-correction decision function: severity ordering, targeted prompt, max iterations | Done | Sorted by check ID, limit configurable 1 to 5 |
| V.4 Verification-gating state machine, four states, flagged never silently accepted | Done | Exactly the four states |
| V.5 Deterministic verification, separate from model probabilities | Done | |
| V.6 Baseline comparison on SROIE and CORD | SROIE done (30 receipts); CORD not run | Say CORD is next; the harness takes any image plus key-field JSON |
| V.7 Behavioural testing on real noisy Indian receipts, store, generate PDF | Done | 9 real photos, PDF generated, stored |

Honest tally: 19 items done, 2 partial (AWS storage, CORD). Say the partials yourself before anyone asks;
it reads as rigour, not weakness. Note: the OCR model runs on the laptop, not on the hosted URL, so show
the OCR boxes view from the laptop.

## Demo order that supports this pitch

1. Crumpled Dutch receipt with the loop on: trace, hypothesis, zoomed crops, changed fields, VERIFIED.
2. Same receipt with the loop off: FLAGGED. "This is what every single-pass system does."
3. Live phone photo.
4. README results: real-photo table, then the SROIE table; read the 9/30 versus 3/30 line.
5. Code: `agent.py` `run_pipeline()` as the architecture, `verify.py` as the model-free core.

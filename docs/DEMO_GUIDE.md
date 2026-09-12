# Demo guide: how to run, what to show, what to say

## 1. Before you walk in

```bash
cd ai_hack
python run.py
```

The terminal prints two URLs. Open the `localhost` one on the laptop and the `http://<ip>:8000` one on
the phone. Phone and laptop must be on the same network: turn on the phone hotspot, connect the laptop
to it, then restart `python run.py` so it prints the new IP.

Checks, 2 minutes before the review:

1. The chip at the top right of the page says `claude-opus-5 · effort medium` (not MOCK MODE). If it says
   the key is missing, the `.env` file with `ANTHROPIC_API_KEY=...` is not in the project folder.
2. Tap **Take photo** on the phone once and shoot anything. If the camera opens, the network path works.
3. Open the Ledger tab and delete test rows you do not want on screen.

Fallbacks:

* No internet or credits: `set MOCK_LLM=1` then `python run.py`. The whole UI runs with a canned receipt.
  Say so openly; do not present it as a live model.
* Phone will not connect: use the sample tiles on the Scan page, they are your own photos.

## 2. Demo script (about 6 minutes)

**Step 1, the crumpled receipt (2 min).** Click the `crumpled1 real` tile. While the stepper runs, say:
"Photo goes through OpenCV cleanup, then a vision model reads it into a strict JSON profile. Now the
verifier runs, pure Python, nine arithmetic and completeness rules." When the result appears, scroll to
the **Self-correction trace**:

* Point at the first *Verification* entry: items did not sum to the total, one amount was unreadable.
* Point at *Targeted re-examination #1*: the hypothesis text, the two zoomed crops, the table of fields
  that changed, and the agent's own sentence explaining what it re-read.
* Click **Attention regions** on the receipt image: the zones it zoomed into pulse.
* Finish on the green **VERIFIED** badge: "Nothing reaches the ledger until the numbers are proven consistent."

**Step 2, the same receipt as a baseline (1 min).** Click **Scan another**, switch off
**Self-correction loop**, click the same tile. It ends **FLAGGED FOR REVIEW**. "Same model, same image,
single pass. This is what every system in our related-work table does. The difference is the loop."

**Step 3, live phone photo (1.5 min).** Hand the phone over, let them photograph any receipt on the
table. Show the result and the PDF or Excel download. If the receipt is clean it will pass first time;
say that is the expected case and the loop only costs time when something is wrong.

**Step 4, the extras (1 min).** Compliance card (policy verdict quoting a rule number), the Ledger tab
with stats and Excel export, and one question in **Ask your ledger**, for example
"total spend by category and which receipts needed correction". Point out the SQL it ran.

**Step 5, evidence (30 s).** Open `README.md` Results: the real-photo table and the SROIE table. Read the
SROIE line "left arithmetically inconsistent 9/30 vs 3/30" aloud; that is the loop's measured effect on a
public benchmark.

## 3. The pitch (90 seconds)

"Receipt extraction is not a reading problem any more, it is a trust problem. Vision language models read
receipts well, but when they misread a digit or skip a tax line they do it confidently and silently, and in
expense accounting one wrong decimal invalidates the record.

Our system treats the receipt's own arithmetic as ground truth. A deterministic verifier checks every
extraction: line items must sum to the subtotal, subtotal plus tax must equal the total, every row must
have an amount, the date must be possible. When a check fails, the agent does not restart. It forms a
hypothesis about what went wrong, for example 'the gap equals the CGST amount, so the paired SGST row was
probably missed', crops the exact region of the image where that value sits, and asks the model to look
again with the hypothesis in the prompt. It repeats until the numbers are consistent or hands the receipt
to a human, never silently accepting it.

On our own phone photos, crumpled and badly lit, the single-pass baseline fails on a third of receipts and
the agent repairs all of them. On the SROIE benchmark it cuts arithmetically inconsistent extractions from
nine in thirty to three. Around that core we built the full workflow the proposal describes: preprocessing,
categorisation, policy compliance, duplicate detection, a ledger you can query in plain English, and PDF
and Excel reports, all from a phone camera in a browser."

## 4. Questions you will get, and answers

* **Why a vision model instead of Tesseract, the proposal said OCR?** Tesseract on a crumpled thermal
  print produces garbage the verifier cannot repair. The vision model is the perception layer and also
  returns the region coordinates; the proposal's OCR box is realised by it. Tesseract can be added as a
  second opinion; it adds no accuracy on these images.
* **If the model is that good, why the loop?** Because "good" is not "verified". SROIE shows the model
  right on totals 97% of the time and still internally inconsistent 30% of the time. The loop is what turns
  a probabilistic read into an auditable record.
* **What is actually new?** Using arithmetic consistency as the trigger and the hypothesis as the prompt,
  and re-examining the *image region*, not just re-generating text. Self-Refine and Reflexion re-read
  their own text output; they never look at the source again.
* **What does the verifier not catch?** Errors that preserve sums, for example a price attached to the
  wrong row. We partially cover this: the model's own low-confidence flags on numeric fields earn one
  targeted re-read, and a missing row amount is an error. Say this plainly; it is the honest limitation.
* **Datasets?** Nine real phone photos with a single-pass baseline, and 30 random SROIE receipts, seed 0,
  results and per-receipt CSV in the repo. CORD is not run yet. Do not claim more than this.
* **Cost and speed?** 15 to 25 seconds for a clean receipt, up to 45 with one correction pass, roughly
  one to three cents of model usage per receipt.
* **AWS?** Storage is behind a three-function interface (save, list, get). SQLite today; the DynamoDB and
  S3 version is a drop-in. Not deployed because it added no evidence for the AI claims.

## 5. Repository tour (what to open when showing code)

```
app/pipeline/agent.py      start here: run_pipeline() is the architecture diagram as code
app/pipeline/verify.py     the nine deterministic checks and their hypothesis strings
app/pipeline/extract.py    every model call: extract, re-examine, categorise, policy, ledger agent
app/pipeline/preprocess.py OpenCV cleanup, receipt crop, deskew, and crop_region (the "zoom")
app/pipeline/models.py     ExpenseProfile, Check, TraceEntry, ReceiptState, and the JSON schema
app/pipeline/report.py, excel.py, storage.py   outputs and persistence
app/main.py, app/static/   web server and the page
tools/                     sample generator, benchmark, SROIE evaluator, preprocessing preview
results/                   evidence: real-photo benchmark logs, SROIE CSV, log, and summary
docs/WALKTHROUGH.md        file-by-file explanation of the pipeline
```

### The SROIE evaluation, explained

* **What SROIE is.** ICDAR 2019 Scanned Receipts OCR and Information Extraction. Task 3 gives, for each
  scanned receipt, four key fields: company, date, address, total. It is the standard receipt benchmark
  and the first row of the related-work table in the proposal.
* **Where the data is.** Not in the repo (626 images, a third-party dataset). `tools/eval_sroie.py`
  expects the public mirror layout `data/img/*.jpg` + `data/key/*.json`:
  `git clone --depth 1 https://github.com/zzzDavid/ICDAR-2019-SROIE`
* **What the script does.** Picks N receipts at random with a fixed seed (reproducible), runs each one
  twice through the same pipeline, once with the loop disabled (baseline) and once enabled (agent), and
  scores total (numeric, 0.01 tolerance), date (normalised to YYYY-MM-DD) and company (token overlap at
  least 0.6). It also records whether each run ended VERIFIED or FLAGGED and how many correction passes ran.
  `--rescore results/sroie_results.csv` recomputes the table from stored values without calling the model.
* **What is in `results/`.** `sroie_results.csv` (every receipt, both modes, all values and flags),
  `sroie_eval_log.txt` (raw run), `sroie_summary.md` (the table plus a line-by-line explanation of each
  miss), and `logs/bench_real.txt` (the real-photo benchmark).
* **How to reproduce.** `python tools/eval_sroie.py <path-to-mirror>/data --n 30 --seed 0`. About 30
  minutes and around 60 model calls.

## 6. If something breaks live

* Result shows an error box: click **Back**, retry once. If it says rate limit or overloaded, use the
  next tile; the API retries itself twice already.
* Page loads but no sample tiles: the server was started from a different folder; run `python run.py`
  from the project root.
* Phone camera button opens the gallery instead of the camera: that is the phone browser's choice, pick
  "Camera" from its sheet.
* Everything is slow: switch `MODEL=claude-sonnet-5` in `.env` and restart; roughly twice as fast, slightly
  less accurate on crumpled receipts.

# Codebase walkthrough

How the project is organised, what each file does, and how to explain the flow of one receipt.

## 1. The one-paragraph explanation

A photo comes in from the phone. OpenCV cleans it up. A vision language model (Claude) reads it into a
strict JSON "expense profile". A deterministic Python verifier applies arithmetic constraints such as
subtotal + tax = total. If any constraint fails, the agent works out *why* it probably failed, cuts out the
suspect region of the image, and asks the model to look again at exactly that region with the hypothesis
in the prompt. It repeats until the numbers are consistent or a limit is hit. Only then does it categorise
the expense, check company policy, check for duplicates, save it, and produce a PDF and Excel report.
The model is never trusted on its own: every number it outputs is checked by code that does not use AI.

## 2. Folder map

```
ai_hack/
├── app/
│   ├── main.py                 web server (FastAPI): upload endpoint, job polling, downloads, ledger, policy, chat
│   ├── static/
│   │   ├── index.html          the single web page (Scan / Ledger / Policy tabs)
│   │   ├── styles.css          styling, responsive for phones
│   │   └── app.js              browser logic: camera, upload, live stepper, result rendering, ledger, chat
│   └── pipeline/               THE AI PIPELINE (this is what to show)
│       ├── models.py           data structures: ExpenseProfile, Check, TraceEntry, ReceiptState, JSON schema
│       ├── preprocess.py       Block 2: OpenCV image enhancement + receipt crop + region crops
│       ├── extract.py          Blocks 3/4/8: Claude calls - extract, re-examine, categorise, policy, ledger agent
│       ├── verify.py           Block 5: deterministic arithmetic/logic checks + hypothesis text
│       ├── agent.py            Blocks 6/7: the self-correction loop and the state machine
│       ├── report.py           Block 10: PDF report (reportlab)
│       ├── excel.py            Excel workbook per receipt and for the whole ledger (openpyxl)
│       └── storage.py          Block 9: SQLite + files, duplicate check, policy text
├── samples/                    test receipts (synthetic + real phone photos)
├── tools/
│   ├── make_samples.py         renders synthetic Indian receipts, clean and degraded
│   ├── benchmark.py            baseline (single pass) vs agent comparison table
│   ├── eval_sroie.py           field accuracy on the SROIE dataset, if downloaded
│   └── preview_preprocess.py   contact sheet of preprocessing output
├── run.py                      start the web app        python run.py
├── run_cli.py                  run one image in the terminal with a full trace
├── requirements.txt / .env     dependencies / API key and model settings
└── data/                       created at runtime: expenses.db, receipts/*.jpg, *.pdf, policy.txt
```

## 3. The life of one receipt, file by file

Follow `run_pipeline()` in `app/pipeline/agent.py`. It is about 100 lines and calls everything else in order.

1. **Preprocess** (`preprocess.py`, `preprocess()`)
   EXIF rotation fix, resize to 1600 px, receipt-aware crop (finds the bright paper in a handheld photo
   so the model gets 2 to 3 times more pixels per character), denoise, deskew by projection profile
   (tries angles from -12 to +12 degrees and keeps the one where text rows line up best), CLAHE
   contrast enhancement, and an adaptive-threshold "machine view" for display.
   Output: three JPEGs plus quality numbers (sharpness, brightness, angle).

1b. **OCR and layout** (`ocr.py`, `run()`), when EasyOCR is installed
   A local neural OCR model (CRAFT detector finds text regions, CRNN recogniser reads them) returns every
   word with a normalised box and a confidence. Three uses: the "OCR boxes" overlay in the UI; the
   agreement check C11, which counts how many of the vision model's amounts the OCR engine independently
   read; and keyword-anchored zones ("total", "GST") that place the zoom crop when the vision model's own
   box fails the ink test. Absent the package, this block is skipped and everything else is unchanged.

2. **Extract** (`extract.py`, `extract()`)
   Sends the enhanced image to Claude with a system prompt describing Indian receipts (CGST/SGST rows,
   DD/MM/YYYY dates, tax-inclusive MRP) and a JSON schema (`EXPENSE_SCHEMA` in `models.py`). The API
   guarantees the answer matches the schema, so it parses straight into `ExpenseProfile`.
   The model also returns approximate bounding boxes for four zones (header, items, taxes, total)
   and a list of fields it was unsure about.

3. **Verify** (`verify.py`, `verify()`) - no AI here, pure arithmetic
   | Check | Rule | Severity |
   |---|---|---|
   | C1 | vendor, date, total present | error if total missing |
   | C2 | sum of line items = subtotal | error |
   | C3 | subtotal + taxes + charges - discount + round-off = total (also detects tax-inclusive pricing, only with evidence) | error |
   | C4 | quantity x unit price = line amount | warning |
   | C5 | tax amounts plausible for the printed rate | warning |
   | C6 | date valid and not in the future (future date with plausible day/month swap = error) | warning/error |
   | C7 | at least one line item | warning |
   | C8 | every line item has an amount | error |
   | C9 | model declared no low confidence on numeric fields (first pass only) | error |
   Each failed check carries a **hypothesis**, e.g. "difference equals the CGST amount, a paired SGST
   row was probably missed" or "total is 10x the expected value, a decimal was dropped".

4. **Self-correct** (`agent.py`, the `while` loop)
   While error-level checks fail and iterations < limit:
   rank failures -> map their fields to image zones (`extract.zones_for`) -> crop and upscale those
   zones (`preprocess.crop_region`; a blank crop falls back to the usual band for that zone) ->
   build the targeted prompt with the previous JSON and the hypotheses (`extract.build_reexamination_prompt`)
   -> send full image + crops + prompt (`extract.reexamine`) -> diff old vs new profile -> verify again.
   Every step is appended to `trace` as a `TraceEntry`, which is what the timeline in the UI shows.

5. **Gate** (`ReceiptState` in `models.py`)
   INITIAL_EXTRACTION -> AWAITING_VERIFICATION -> FLAGGED_FOR_REEXAMINATION (loop) -> VERIFIED or
   FLAGGED_FOR_REVIEW. A receipt that never becomes consistent is kept and marked, never silently accepted.

6. **Categorise and comply** (`extract.categorize`, `extract.check_policy`, `agent.check_duplicate`)
   Two small text-only model calls (12 accounting categories; policy verdict quoting rule numbers) and
   a deterministic duplicate check against the ledger (same date, same total, overlapping vendor name).

7. **Persist and report** (`storage.save`, `report.build_pdf`, `excel.build_receipt_xlsx`)
   SQLite row with the full JSON, image and PDF on disk, Excel on demand.

## 4. Where the "novelty" lives, if asked

* `verify.py` is the deterministic reasoning core. It is the reason hallucinated numbers cannot pass.
* `agent.py` lines around the `while` loop are the closed loop the related-work table says other systems lack.
* `preprocess.crop_region` + `extract.reexamine` are the "direct attention to uncertain regions" mechanism.
* The `uncertain_fields` -> C9 path is the model's own self-doubt being turned into a verification signal.

## 5. Useful things to run live

```bash
python run_cli.py samples/crumpled1_real.jpg              # prints checks, trace, changed fields
python run_cli.py samples/crumpled1_real.jpg --no-correct # same receipt, baseline: ends FLAGGED
set MOCK_LLM=1 && python run.py                            # whole UI with no API calls
python tools/benchmark.py samples/*_real.jpg              # comparison table
```

## 6. Evaluation status (be precise about this)

* Tested on 9 real phone photos and 8 synthetic receipts (4 clean, 4 degraded). Results in `bench_real.txt`.
* SROIE and CORD, named in the proposal, have **not** been run yet. `tools/eval_sroie.py` is ready for
  SROIE key-field accuracy (company, date, address, total) once the dataset is downloaded.
* The single-pass baseline in the benchmark is the same model with the loop disabled, which isolates
  the contribution of the self-correction loop.

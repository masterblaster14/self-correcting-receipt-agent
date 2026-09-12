# Receipt Agent — Self-Correcting Multimodal Expense Extraction

Photograph a receipt from your phone; a vision model extracts a structured expense profile, a
deterministic verifier checks every arithmetic constraint, and when the numbers do not add up the
agent zooms back into the suspect region of the image and re-examines it — until the extraction is
mathematically consistent or it is flagged for human review. Output: a verified JSON profile, a
category, a policy-compliance verdict, and a PDF expense report.

Team: 24BIT0316 Om Gunjan Gupta · 24BIT0309 Abhishek Kumar Singh

**Live demo:** https://web-production-66c2c.up.railway.app (open on a phone and tap *Take photo*)

## Quick start

```bash
pip install -r requirements.txt
copy .env.example .env      # put your ANTHROPIC_API_KEY in .env
python run.py
```

Open `http://localhost:8000` on the laptop. The terminal also prints a `http://<lan-ip>:8000`
URL — open that on a phone connected to the same Wi-Fi (or to the laptop's hotspot) and tap
**Take photo**. No HTTPS needed: the page uses the native camera file picker.

Offline demo without an API key:

```bash
set MOCK_LLM=1 && python run.py
```

Command line:

```bash
python run_cli.py samples/cafe_clean_bad.jpg              # full agent
python run_cli.py samples/cafe_clean_bad.jpg --no-correct # single-pass baseline
python run_cli.py samples/cafe_clean.jpg --fault --pdf out.pdf
python tools/make_samples.py                              # regenerate synthetic receipts
python tools/benchmark.py samples/*_real.jpg              # baseline vs agent table for the report
python tools/eval_sroie.py <sroie-root> --n 30            # SROIE key-field accuracy (dataset not included)
python tools/preview_preprocess.py                        # contact sheet of preprocessing output
```

See `docs/WALKTHROUGH.md` for a file-by-file explanation of the pipeline.

Outputs per receipt: JSON profile, PDF report (`/api/receipts/{id}/pdf`), Excel workbook with Summary /
Line items / Verification / Agent trace sheets (`/api/receipts/{id}/xlsx`). Whole ledger as Excel with
Receipts / Line items / By category sheets: `/api/export.xlsx` (button on the Ledger tab).

## Results

**Real phone photos (9 receipts, `samples/*_real.jpg`, `results/logs/bench_real.txt`).** Same model
with the loop disabled is the baseline.

| Receipt | Single-pass baseline | Self-correcting agent |
|---|---|---|
| crumpled1 (Dutch café, badly crumpled) | FLAGGED: item sum ≠ total, one amount unreadable | VERIFIED after 1 pass — recovered the folded last row from the zoomed crop, recognised VAT-inclusive pricing |
| foodcy (handheld) | FLAGGED: date read as 8 Nov 2026 (future) | VERIFIED after 1 pass — re-read as 11 Aug 2026 (MM/DD) |
| crumpled (US pizzeria) | VERIFIED but modifier row took the next item's price | VERIFIED — model-declared uncertainty triggered a re-read; rows now correct |
| other 6 | VERIFIED first pass | VERIFIED first pass, no extra calls |

**ICDAR-2019 SROIE, 30 random receipts (seed 0), key-field accuracy.** `results/sroie_results.csv`, log in
`results/sroie_eval_log.txt`. Run with `python tools/eval_sroie.py <sroie-root> --n 30`.

| Metric | Single-pass baseline | Self-correcting agent |
|---|---|---|
| total correct | 29/30 (97%) | 29/30 (97%) |
| date correct | 30/30 (100%) | 30/30 (100%) |
| company correct | 27/30 (90%) | 28/30 (93%) |
| receipts left arithmetically inconsistent (flagged) | 9/30 | 3/30 |
| receipts where a correction pass ran | n/a | 10/30 |

Reading of the SROIE numbers, honestly: these are clean flatbed scans, so a strong vision model already
reads the headline fields correctly in one pass and the loop cannot add much on `total`/`date`. What the
loop does change is *internal consistency*: 9 of 30 single-pass extractions violate an arithmetic
constraint (line items vs subtotal, tax, rounding) and the agent resolves 6 of them. The remaining
misses are: one total where the ground truth is the pre-rounding figure (72.93) and the model returned
the amount payable printed below it (72.95); two company names where the ground truth is the legal
entity and the model returned the trade name printed in the header; and one ground-truth typo
(`TAII ORING`). The self-correction loop earns its keep on degraded phone photos (table above), which is
the setting the proposal targets.

Typical latency with Claude Opus 5 at medium effort: 15–25 s for a clean receipt, 30–45 s when one
correction pass runs.

## How it maps to the proposal

| Block | Where |
|---|---|
| 1 Upload | `app/static/` (camera / upload / drag-drop / paste) → `POST /api/process` |
| 2 Image preprocessing (OpenCV) | `app/pipeline/preprocess.py` — EXIF orient, resize, denoise, deskew, CLAHE, adaptive threshold |
| 3–4 Extraction (multimodal LLM) | `app/pipeline/extract.py` — Claude vision, strict JSON schema in `models.py`, returns field regions |
| 5 Verification engine (deterministic) | `app/pipeline/verify.py` — 7 checks, hypothesis generation, tax-inclusive detection |
| 6 Self-correction agent | `app/pipeline/agent.py` — ranks failures, builds targeted prompt, crops suspect regions, re-examines, re-verifies, max N iterations |
| 7 Verification-gating state machine | `ReceiptState` in `models.py`: INITIAL → AWAITING_VERIFICATION → FLAGGED_FOR_REEXAMINATION → VERIFIED / FLAGGED_FOR_REVIEW |
| 8 Categorisation (LLM) | `extract.categorize()` + policy compliance `extract.check_policy()` |
| 9 Storage | `app/pipeline/storage.py` — SQLite + files (interface is drop-in replaceable with DynamoDB/S3) |
| 10 PDF report | `app/pipeline/report.py` (reportlab); Excel per receipt + whole-ledger export in `app/pipeline/excel.py` (openpyxl) |
| 11 Output | Result screen, Ledger tab, PDF download, natural-language ledger queries |

## Deploying to Railway (public HTTPS URL, works from any phone)

The repo already contains `Procfile`, `railway.json` and `runtime.txt`; OpenCV is the headless build so no
system packages are needed.

```bash
npm i -g @railway/cli            # once
railway login
railway init                     # create a project (or `railway link` to an existing one)
railway variables --set ANTHROPIC_API_KEY=sk-ant-... --set MODEL=claude-opus-5 --set EFFORT=medium
railway up                       # build + deploy from this folder
railway domain                   # generate the public https URL
```

Optional but recommended: persistent storage, otherwise the ledger resets on every redeploy.

```bash
railway volume add --mount-path /data
railway variables --set DATA_DIR=/data
```

Optional email: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` the same way.
On Railway the phone camera works through the normal HTTPS page, no hotspot needed.

## Creating an SMTP account (for emailed reports)

Any SMTP provider works; the app just needs host, port, username, password.

**Gmail (fastest):** the Google account must have 2-Step Verification on. Google Account → *Security* →
*2-Step Verification* → scroll to *App passwords* → create one named "Receipt Agent" → copy the 16-character
password. Then:

```
SMTP_HOST=smtp.gmail.com  SMTP_PORT=587  SMTP_USER=you@gmail.com  SMTP_PASS=<16-char app password>  SMTP_FROM=you@gmail.com
```

**On Railway's trial plan outbound SMTP ports are blocked**, so Gmail SMTP fails there with "Network is
unreachable". Use Brevo's HTTPS API instead (free, 300 emails/day): sign up at brevo.com, verify your Gmail
as a sender under *Senders & IP*, generate an API key under *SMTP & API*, then set `BREVO_API_KEY=xkeysib-...`
and `SMTP_FROM=<the verified address>`. When `BREVO_API_KEY` is present it is used instead of SMTP.

## Connecting GitHub to Railway for automatic deploys

Railway deploys on every push once its GitHub app can see the repository. This is a one-time authorisation
on your accounts:

1. https://railway.com/account/integrations → *GitHub* → *Configure* → install the Railway app on
   `masterblaster14` and grant access to `self-correcting-receipt-agent` (all repos or just this one).
2. In the Railway project → the `web` service → *Settings* → *Source* → *Connect Repo* → pick the repo,
   branch `main`. Keep the existing variables and volume. Alternatively from the CLI:
   `railway add --service app --repo masterblaster14/self-correcting-receipt-agent` and move the domain and
   volume to the new service.
3. Optional: enable *Wait for CI* and *Check suites* if you add tests later.

Until that is done, deploy manually with `railway up --service web` from this folder.

## Organisation, people and email

The **Organisation** tab holds the organisation name, a finance mailbox, the people directory
(name, email, department, manager email) and the expense policy. Choose a person under **Submitting as**
on the Scan page; the receipt is stored against them and their department, the Ledger can be filtered by
either, and **Email report** sends the PDF and Excel to their manager, to finance and to them.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required unless `MOCK_LLM=1` |
| `MODEL` | `claude-opus-5` | vision model; `claude-sonnet-5` is cheaper for bulk testing |
| `EFFORT` | `medium` | `low` / `medium` / `high` reasoning effort per call |
| `MOCK_LLM` | `0` | `1` = canned responses, no API calls |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `SMTP_FROM` | — | enable **Email report** (Gmail: use an App Password; blocked on Railway trial, see below) |
| `BREVO_API_KEY` + `SMTP_FROM` | — | enable **Email report** over HTTPS (works on Railway) |
| `DATA_DIR` | `./data` | where SQLite, receipt images and PDFs are stored (Railway volume: `/data`) |

## Project layout

```
app/
  main.py              FastAPI app + job polling API
  pipeline/
    models.py          ExpenseProfile, checks, trace, state machine, JSON schema
    preprocess.py      OpenCV enhancement + region cropping
    extract.py         Claude calls: extract, re-examine, categorise, policy, ledger agent
    verify.py          deterministic arithmetic / logic constraints + hypotheses
    agent.py           the self-correction loop and orchestration
    report.py          PDF generation
    storage.py         SQLite + file persistence, duplicate detection, policy text
  static/              single-page frontend (index.html, styles.css, app.js)
samples/               synthetic test receipts (clean + degraded)
tools/make_samples.py  generator for the above
run.py / run_cli.py    web server / command-line runner
```

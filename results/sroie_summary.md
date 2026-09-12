# SROIE evaluation (ICDAR-2019 SROIE task 3 key fields)

Subset: 30 receipts, random with seed 0, from the public mirror `zzzDavid/ICDAR-2019-SROIE`
(`data/img`, `data/key`). Model: claude-opus-5, effort medium. Baseline = same model, loop disabled.
Command: `python tools/eval_sroie.py <root> --n 30 --seed 0 --out results/sroie_results.csv`

| Metric | Single-pass baseline | Self-correcting agent |
|---|---|---|
| total correct | 29/30 (97%) | 29/30 (97%) |
| date correct | 30/30 (100%) | 30/30 (100%) |
| company correct | 27/30 (90%) | 28/30 (93%) |
| receipts left arithmetically inconsistent (flagged) | 9/30 | 3/30 |
| receipts where a correction pass ran | n/a | 10/30 |

Scoring: total = numeric match within 0.01; date = normalised to YYYY-MM-DD; company = token overlap
>= 0.6 with the ground truth. Address is not scored (layouts vary too much for exact match).

## Misses, receipt by receipt

| File | Field | Ground truth | Model | Note |
|---|---|---|---|---|
| 146.jpg | total | 72.93 | 72.95 | receipt prints "Total 72.93" and "TTL ATF RND 72.95"; model returned the amount payable |
| 250.jpg | company | GL HANDICRAFT & TAII ORING | GL HANDICRAFT & TAILORING | ground-truth typo |
| 519.jpg | company | ESJAY FUEL ENTERPRISE | BHPetrol Permas Jaya 2 (Esjay Fuel Enterprise) | trade name vs legal entity; agent added the legal entity, baseline did not |
| 396.jpg | company | FOUR QUARTERS SDN BHD | A PIE THING | trade name vs legal entity |

Per-receipt values, states and iteration counts: `sroie_results.csv`. Raw run log: `sroie_eval_log.txt`.

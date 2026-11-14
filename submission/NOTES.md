# Temporal knowledge system: notes

**API keys: none.** The solution uses only the Python standard library, makes no network calls, and gives the same answer every time. `requirements.txt` is empty on purpose.

Run it:

```bash
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl
```

Ingest takes about 6 s for all five snapshots. Answers take about 0.1–0.25 s each (p95).

## How it works

**1. Parse every snapshot into small units.** Each markdown table row becomes a *row unit*. It keeps the table caption (the nearest headings and short lines above it), the column headers, the cells, and the "Rates are effective as of …" date printed under the table. The rest of the page is split into short *text units* of one to a few paragraphs, each tagged with its section heading. The parser also records every in-site link (with its anchor text and section heading), every branch-hours block (Lobby / Drive-up), and a page's publication date for newsroom and blog posts.

**2. Keep history, not just the latest copy.** Each unit records the snapshots it appeared in. When a page is missing from a later crawl, its newest version stays visible but is down-weighted. Every question is answered from one snapshot view: the latest snapshot on or before `as_of`, or the newest when no date is given. If the question itself names a date ("as of May 2026", "in June 2026") and no `as_of` is passed, that date is used.

**3. Decide what is "current".** Units are retrieved with BM25 and re-ranked by these rules:

| Rule | Effect |
|---|---|
| Page authority | `/rates` and `/business-rates` (1.0) > product pages under `/personal`, `/business`, `/about`, `/contact` (0.9) > other pages (0.75) > newsroom (0.6) > blog (0.5) > landing pages (`-lp`, `switch-and-earn`, `homebuying-guidance`) (0.45) > PDFs (0.4) > calculators (0.35) > tag and author listings (0.15). |
| Recency | Newer press releases beat older ones. For counts ("how many branches") the newest dated source wins, so 22 beats 23 and 24. |
| Boilerplate | Text repeated on many pages is down-weighted. |
| Expired offers | Prose saying an offer "ended" or "expired" is down-weighted. |
| Table freshness | Among copies of the same table row, the most authoritative page with the newest "effective" date is used. Stale copies (calculators, landing pages) feed `conflict_note` and `conflicts()` and are never quoted. |

**4. Respect qualifiers.** Hard checks on rows:

| Qualifier | How it is checked |
|---|---|
| Term | 6 / 12 / 60-month and 15 / 30-year must match. |
| Vehicle model year | Must fall inside the row's range (2025 → "2024-27"). |
| Balance or loan amount | Must fall inside the tier ("$10,000 – $24,999.99"). |
| Product variants | Jumbo, IRA (Roth / Traditional), business, refinance, intro or balance-transfer offers and Mini-Max are penalised unless the member asked for them. |
| Unrequested product words | Words in the row label that the member didn't ask for ("High-Yield", "Business") lower the score. A plain "savings" question means Share Savings. |

**5. Answer briefly and extractively.** The answer is the matching row(s) with the effective date, or the best one or two sentences. Stale numbers elsewhere on a page are never repeated, so a correct answer can't be zeroed by a trap value.

| Question type | What is returned |
|---|---|
| "highest" / "lowest" | The top row of the table. |
| List questions ("credit cards and their APRs", "mortgage rates") | Whole tables, from authoritative pages only. |
| "Do any savings tiers pay 0.35%?" | The claim is checked against the table. |
| Hours | Taken from branch-hours blocks. The general answer gives the most common lobby and drive-up schedules and asks which branch. A named branch with no published hours gets an honest "not listed". |
| Links | Only in-site links that still resolve in the snapshot, ranked by how often they're linked, their anchor text, and whether the target page holds the asked-for content. |
| "When…" / "how many…" / phone and routing numbers | The sentence that contains the asked-for slot (a date, a count, a number). |
| Eligibility and contact-update intents | Short templated answers built from the join page and the main phone number. |

**6. Be honest.** If no published table row or rate sentence matches the product, the answer is "I don't know: the website doesn't list …" with `confident=False` and no numbers. Examples: a 7/1 ARM, a construction-loan rate, a renters-insurance premium, hours for a branch the site doesn't list. Four rules keep this robust to casual wording:

- Only words that name products count. A built-in list of common English words ("house", "teenager", "sheet", "altogether"…) is ignored, and product words (home, card, car, checking, wire…) are kept.
- Coverage is checked against the vocabulary of priced tables (row labels and captions) and of product-page headings.
- If the question names a product that the site describes but never prices ("adjustable-rate mortgage", "construction loan", "ARM", "Sweeps"), the system never answers from an unrelated table. It falls back to a prose sentence that states the figure, or it refuses.
- Questions that ask for a specific kind of value need a sentence that contains one. A fee needs a dollar amount, a credit score needs a 3-digit score, a cutoff time needs a clock time, a count needs a number. Without one the answer is "I don't know".

A loan question is never answered from a deposit table, and a deposit question is never answered from a loan table.

## conflicts()

Each row is mapped to a fact key: product family (auto, mortgage-purchase, special certificate, jumbo, IRA, …), term, amount tier, and model-year *rank*. The rank is used because "2023–26" on an old calculator table and "2024-27" on the current rate sheet are the same tier. When one fact key has different values across pages or snapshots, it becomes a conflict. The resolution is the value on the most authoritative page in the newest crawl, and the reason says which pages are stale and how the value changed over time.

The list also includes short prose facts ("6-month certificate APY: 3.50%") with expired promotions demoted, branch-count contradictions (22 vs 23 / 24 / 18), and press-release ordinals (Forbes "fifth" vs "fourth").

## Development checks

`dev_eval/` holds extra question sets in the public-eval format. They are for development only; graders use their own hidden set.

| File | What it holds |
|---|---|
| `paraphrase_checks.jsonl` | Rewordings and as-of variants |
| `heldout_gpt_batch1.jsonl` | 45 items written independently from the snapshots, each value checked by grep |
| `heldout_gpt_batch2_casual.jsonl` | 50 casual, indirect member phrasings |

Run any of them the same way as the public set:

```bash
python harness/run_eval.py --submission submission --eval submission/dev_eval/heldout_gpt_batch1.jsonl
```

## Known limits

- Fully extractive, with no language model, so answers are sometimes terse or quote a slightly wordier sentence than a person would.
- Questions phrased far from the site's own wording can miss a passage. The refusal rule then prefers "I don't know" over guessing.

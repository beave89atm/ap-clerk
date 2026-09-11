# QUALITY V1.2

Treyce’s 2026-09-10 notes on the 8/16 dry-10 sheet, plus Kyle’s never-repeat and
**Treyce-load / fix-before-complete** rules. Code + unit tests only. **Do not**
run a live KIMCO or mailbox job until Kyle reviews.

**Hard email cap 10 until further notice (Kyle 2026-09-11).** Cap = mailbox
messages *touched* (Success, HOLD, Incomplete, Fail, Skipped/noise). Stop after
10 emails. Do **not** walk past noise to fill N bill attempts. Bill-attempt mode
is suspended until Kyle lifts this. Weekday/daily `--limit 30` is hard-clamped
to 10. Still note each of those ≤10 on the sheet with Why.

**Skip already-flagged (Kyle 2026-09-11).** Leave alone — do not reprocess, do
not re-stamp categories — if the message already has Outlook `Entered in AI`,
`AI HOLD`, `Entered with issues`, or Graph `flag.flagStatus=flagged`. Those
messages do **not** consume the 10-email touch cap. Only unflagged /
uncategorized (by those AP markers) messages count.

`Success` means a **finished bill Treyce would not need to rework** — not a
header create, not a close-enough line match, not Fees miscoded as PPV.

## Outlook categories (exactly three)

| Result | Outlook |
| --- | --- |
| **Success** (finished; Treyce would not rework) | `Entered in AI` |
| Header + PDF entered but bill cannot be finished (price-does-not-match, qty HOLD, Incomplete finish) | `Entered with issues` |
| Real bill unprocessable **without** a header (parse-error / no-pdf, auto-pay, pdf-behind-link, Fail) | `AI HOLD` |
| Noise (not-a-bill, statement, CHECK STOP notice, payment, POD, duplicate) | none — Excel `Skipped` only (PR #19) |

**Kyle action:** create the Outlook master category named exactly `Entered with issues` on `accountspayable@kannonmfg.com` if it does not exist. Code POSTs the category and PATCHes the exact string; a deny/missing apply is recorded with a clear Why.

Never a fourth process-category name. Never `Entered in AI` + another process marker on the same message.

## Treyce-load: fix-before-complete checklist

Before any `Success`, `treyce_finish_selfcheck` / `finish_gate(..., selfcheck=)` runs this list
(`ap_clerk/quality_v12.py` `TREYCE_FINISH_CHECKLIST`):

1. **Invoice # from PDF** — including Techni-Tool-style suffixes (`S1387370.001`).
2. **PO on PDF → not blank Type 4** — set PO, Invoice_Type 3, Select Receipts.
3. **Receipt line by part/description** — not the first leftover qty (O’Neal SCH 40 A500).
4. **Qty invoice vs PO/receipt equal** — else HOLD for the buyer (Capital 26764).
5. **Fees/surcharges → Fees and surcharges** — never PPV (Techni-Tool $46.20).
6. **PPV only** for unit-price gaps vs PO, and only if ≤10% of invoice total **and** ≤$100; else price-does-not-match HOLD + `@Shawn McKibben`.
7. **Vendor PDF attached** on the header.
8. **Select Receipts posted** when the PO path applies.

If Treyce would still fix header, lines, or charges → **HOLD**, **Incomplete**, or **Entered with issues**. Never `Success`.

Every non-Success **Why** must name the gate and the next action so she is not hunting.

## Never-repeat regressions (Treyce 8/16)

Named tests in `tests/test_quality_v12.py`. Registry: `ap_clerk/quality_v12.py`.

| Note | 8/16 miss | Expected gate | Test |
| --- | --- | --- | --- |
| **NOTE-01** Insight 1809; MSC/Rob Brown 5157357 vs filename 191471 | False parse-error HOLD / filename invoice # (`no-pdf-on-vm`) | `preflight-parse` — # from PDF; empty PDF → HOLD no-pdf, never Success | `test_note01_insight_msc_pdf_invoice_number` |
| **NOTE-02** Techni-Tool `S1387370.001`; $46.20 Fees | Bare `S1387370`; $46.20 as PPV (fake Success) | Suffix from PDF; Fees not PPV; never Success if miscoded | `test_note02_technitool_suffix_and_fees_not_ppv` |
| **NOTE-03** Capital 26764 qty 2 vs 2.5; $25 Fees | Claimed Success | `qty-does-not-match` HOLD + Fees $25; never Success | `test_note03_capital_qty_discrepancy_hold` |
| **NOTE-04** Purvis 32625214 / PO 58926 | Blank Invoice_Type 4 | `po` — Type 3 + PO set; never Success as Type 4 | `test_note04_purvis_po_never_type_4` |
| **NOTE-05** O’Neal PIPE A500 → `P-1.00 SCH 40-A500` line 3 | Wrong PO line (first qty) | `receipt` — description/part match; ~$0.03 may be PPV; wrong line never Success | `test_note05_oneal_description_line_match` |
| **NOTE-06** EMJ large price gap | HOLD without header/PDF | `price-does-not-match` HOLD + header + PDF + `Entered with issues`; never Success | `test_note06_emj_price_hold_header_entered_with_issues` |
| **NOTE-07** Toyota Commercial Finance / auto-pay | Entered as a PO bill | `auto-pay` HOLD; no ERP header; never Success | `test_note07_toyota_autopay_hold` |
| **NOTE-08** Melody Channell invoices | Junk not-a-bill skip | Bill, not noise Skipped; never Success-as-skip | `test_note08_melody_channell_not_noise` |
| **NOTE-09** AQPC link-download PDF | Silent not-a-bill | Best-effort https GET; auth → `pdf-behind-link` HOLD; never Success | `test_note09_aqpc_pdf_behind_link` |
| **NOTE-10** Gas & Supply Misc vs CHECK STOP | Blanket CHECK STOP | Invoice pages → Type 4 `Shop Supplies - G&S`; notice skip; ambiguous HOLD; never Success | `test_note10_gas_supply_misc_vs_check_stop` |

### Deferred (failing-safe stubs — not silent skips)

| Note | Limitation | Gate if we cannot finish |
| --- | --- | --- |
| NOTE-09 | Vendor portal / login cookie download is not implemented | HOLD `pdf-behind-link` with Why |
| NOTE-10 | Live `0040367887` 5-invoice amount split needs that PDF | HOLD `preflight-parse` when `gas_misc_ambiguous` |

## Unchanged (Kyle / prior PRs)

- **Hard email cap 10 until further notice (Kyle 2026-09-11)** — replaces “cap = N bill attempts / walk past noise” (PR #19). Noise is still Excel `Skipped` without Outlook `AI HOLD`, but noise **consumes** the 10-email touch cap.
- **Skip already-flagged** — `Entered in AI` / `AI HOLD` / `Entered with issues` / `flag.flagStatus=flagged` are walked past without touching and do not consume the cap.
- One Mail.Send per run after the final sheet only.
- No auto-pay. No auto-close batch. Payments human-gated.
- Shawn McKibben comments on price-does-not-match.
- Success = finished bill only (now also: Treyce would not rework it).

Pause further dry runs until Kyle reviews this V1.2 PR.

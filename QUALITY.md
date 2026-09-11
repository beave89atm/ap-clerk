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

**NOTE-11 Nova Alloys 258145 (8/18 dry-10, Kyle 2026-09-11).** Vendor is the
company on the PDF (`Nova Alloys`), never the From person’s name
(`Erica Barrett`). **PDF-is-truth:** the same # on the subject is a hint —
do not HOLD preflight-parse for a `subject` tag when the PDF is on disk;
create the header and attach. Do not report `no-pdf-on-vm` when the file
exists; OCR/retry if text extract fails. Only HOLD no-pdf if the PDF is
truly missing or extract+OCR failed. **Why-must-be-true** for leftover HOLDs.

**NOTE-12 MSC 70762501 posted as RMP (8/18 dry-10).** Parsed vendor
`MSC Industrial Supply` must not `names_match` `RMP INDUSTRIAL SUPPLY` (generic
tokens `industrial`/`supply` are not enough). Prefer known alias MSC→128 over
fuzzy sample seeding. After header create, GET the invoice: if the posted
vendor name/id is not the parsed vendor (or a known alias for that same
vendor), Result is **not Success** — HOLD / Entered with issues, Why
`vendor-mismatch` (parsed X, posted Y). Do not void. 8/18 wrote KIMCO **9967**
as `1320-RMP INDUSTRIAL SUPPLY` type 4.

**NOTE-13 Crosslink 27943 / 27944 / 27946 (8/18 dry-10).** Same false
preflight-parse HOLD as Nova 258145, but the invoice # was tagged `filename`
(`invoice-27943.pdf`) while date/amount/PO came from the PDF. Subject
`Invoice #27943 for 58888 (#8221) from Crosslink Powder Coating`. **PDF-is-truth:**
do not HOLD preflight-parse when the PDF is on disk and the subject or filename
contains the same invoice # — create header, attach, continue. Why must name
Crosslink / 27943 / filename / PDF-on-disk, never `(MSC/McQueary)`.
`no-pdf-on-vm` is forbidden when `pdf_path` exists. Same for 27944 / PO 58909
and 27946 / PO 58741. Fees `Packaging/Shop Supplies; Recovery` stay Fees, not PPV.

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
   Parsed fee amounts must be **posted** as Additional Charge Fees and
   surcharges / F-Fees & Surcharges before Success (Fastenal TXFT4100079
   Shipping & Handling 63.98). Sheet Fees column is not a post.
6. **PPV only** for unit-price gaps vs PO, and only if ≤10% of invoice total **and** ≤$100; else price-does-not-match HOLD + `@Shawn McKibben`.
7. **Vendor PDF attached** on the header.
8. **Select Receipts posted** when the PO path applies. Receipt qty and
   merchandise cost must match the invoice. Never first-open /
   second-open-on-po when multiple open receipts differ (Fastenal
   TXFT4100079 qty 36 vs invoice 35).
9. **Posted vendor matches parsed** — GET after create; posted name/id is the
   parsed vendor or a known alias. Else HOLD `vendor-mismatch`. Never Success.

If Treyce would still fix header, lines, or charges → **HOLD**, **Incomplete**, or **Entered with issues**. Never `Success`.

Every non-Success **Why** must name the gate and the next action so she is not hunting.

## Why-must-be-true

Sheet **Why** must be true for **this** invoice. Treyce cannot tell what failed
when Crosslink / Nova / Telecom / Insight HOLDs reuse an MSC/McQueary slogan.

Every parse HOLD Why includes:

1. **Vendor** on this bill
2. **Invoice #** on this bill
3. **Source of the #** — `pdf` / `subject` / `filename`
4. **Whether the PDF path existed** on the VM (`PDF on disk` vs `PDF path missing`)
5. **Next action** (obtain the PDF, retry OCR, read Amount Due from the PDF, …)

Never append `(MSC/McQueary)` or any other historical case name unless this
invoice is actually that vendor. If the PDF is on disk, Attach status and Why
must not say `no-pdf-on-vm`. Leftover preflight Why strings after the
NOTE-11/13 parse-HOLD relaxation still follow this rewrite (true missing PDF,
OCR failed after retry, unverified PDF total).

## PDF-is-truth

The vendor PDF is the version of the truth. Subject line and filename are
**hints only**.

- PDF wins for vendor, invoice #, date, PO, amounts, fees, and lines.
- If a PDF is attached / on disk, do **not** HOLD `preflight-parse` just
  because the # was also on the subject or filename, or because
  `field_sources` tagged `subject` / `filename`.
- With a PDF present: create the header, attach the PDF, and continue Select
  Receipts / finish gates as usual.
- HOLD parse / no-pdf only when the PDF is **truly missing**, or extract+OCR
  of that PDF failed.

## Never-repeat regressions (Treyce 8/16 + Kyle 8/18 Nova / MSC / Crosslink + 9/11 Fastenal / EMJ)

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
| **NOTE-11** Nova Alloys 258145 / From Erica Barrett (8/18) | Vendor=`Erica Barrett`; HOLD preflight-parse (`invoice #` tagged `subject`); Attach `no-pdf-on-vm` though PDF was on disk | PDF-is-truth: Vendor=Nova Alloys; same # on subject is OK; create header+attach; Why describes THIS bill (no MSC/McQueary); only HOLD no-pdf if file missing | `test_never_repeat_nova_258145` |
| **NOTE-12** MSC 70762501 / KIMCO 9967 posted as RMP (8/18) | Parsed MSC Industrial Supply; Result Success; live GET `1320-RMP INDUSTRIAL SUPPLY` type 4 | MSC ≠ RMP (distinctive tokens); alias 128 over fuzzy seed; GET posted vendor must match or HOLD `vendor-mismatch`; never Success | `test_never_repeat_msc_70762501_not_rmp` |
| **NOTE-13** Crosslink 27943 / 27944 / 27946 (8/18) | HOLD preflight-parse (`invoice #` tagged `filename`); Attach `no-pdf-on-vm` though `invoice-27943.pdf` was on disk | PDF-is-truth: filename # + PDF on disk is not a parse HOLD; create header+attach; Why describes THIS Crosslink bill (no MSC/McQueary); no-pdf-on-vm forbidden when file exists | `test_never_repeat_crosslink_27943` |
| **NOTE-14** Fastenal TXFT4100079 / PO 58692 / KIMCO 9968 (Kyle 2026-09-11) | Select Receipts qty **36** (first open on PO) vs invoice **35**; Why said “not first qty”; Shipping & Handling 63.98 Excel-only, never Additional Charge | Verify qty/cost; pick 35 not 36; never first-open / second-open-on-po Success when open receipts differ; post F-Fees & Surcharges 63.98 before Success or Incomplete / Entered with issues | `test_never_repeat_fastenal_txft4100079` |
| **NOTE-15** EMJ Z250725432 / PO 58913 / KIMCO 9969 (Kyle 2026-09-11) | Two invoice lines; `lines:[]` then one PO receipt; Success; skipped line not on sheet; random-length gap not PPV | Parse both lines; Select Receipts per line; unmatched → not Success + Why names the skipped line; small length variance is PPV (≤10% / ≤$100), not Fees; prepaid/ship-date null amount is not a fee | `test_never_repeat_emj_z250725432_two_lines` |
| **NOTE-16** Gas `billing01_A3050_c.pdf` / 0040370068 / KIMCO 9970 (Kyle 2026-09-11) | 6 invoices collapsed to one # + multi-PO Incomplete; amount 322 was before tax | Split to 6 bills; after-tax Amount Due (never Subtotal/Merchandise); page-range PDF per invoice when feasible else full pack + Why `multi-invoice-pdf page X–Y of N`; email still 1 touch; never Success/Incomplete a collapsed pack or a pre-tax amount | `test_never_repeat_gas_multi_invoice_pdf` / `test_never_repeat_gas_after_tax_amount` |
| **NOTE-17** Insight 1809 already entered (Kyle 2026-09-11) | HOLD preflight-parse MSC/McQueary + `no-pdf-on-vm`; never said already-entered | Duplicate check before parse-HOLD; HOLD `already-entered` names vendor / # / existing KIMCO id(s); no McQueary; no `no-pdf-on-vm` when PDF is on disk | `test_never_repeat_insight_1809_already_entered` |
| **NOTE-18** Outlook `AI Skipped` for noise (Kyle 2026-09-11) | Noise was sheet-only; no Outlook category; already-flagged ignored `AI Skipped` | Stamp exact `AI Skipped` (never `AI HOLD`); Why `outlook-category-missing: AI Skipped` if Graph cannot apply; already-flagged includes `AI Skipped` | `test_never_repeat_ai_skipped_noise` |
| **NOTE-19** 3P / Rachel Bailey INV# 142041 multi-PO (8/18) | Skipped not-a-bill | Invoice, not noise; header PO blank; Select Receipts per PO; unmatched PO named on Why; never silent Success | `test_never_repeat_3p_rachel_bailey_not_noise` / `test_3p_multi_po_select_receipts` |
| **NOTE-20** Eastern Metal 818600 / 818601 (8/18) | Skipped not-a-bill | Invoice + Eastern Metal (alias 64) is a bill; invoice hint or PDF → never not-a-bill | `test_never_repeat_eastern_metal_818600_not_noise` |
| **NOTE-21** AQPC 10917 / 10918 payment-request link (8/18) | Skipped not-a-bill + `no-pdf-on-vm` | Download https invoice link; auth wall → HOLD `pdf-behind-link` names vendor / # / host | `test_never_repeat_aqpc_10917_link_download` |
| **NOTE-22** KIMCO vendor + invoice never skip (Kyle) | Listed vendors with Invoice/INV subjects were Skipped | KIMCO vendor + invoice (PDF, link-PDF, or Invoice/INV subject) → enter or HOLD; AI Skipped only for true noise | `test_never_repeat_kimco_vendor_invoice_never_skip` |

### Deferred (failing-safe stubs — not silent skips)

| Note | Limitation | Gate if we cannot finish |
| --- | --- | --- |
| NOTE-09 | Vendor portal / login cookie download is not implemented | HOLD `pdf-behind-link` with Why |
| NOTE-10 | Shared-total-only Gas packs (no per-invoice Amount Due) | HOLD `preflight-parse` when `gas_misc_ambiguous` |

## Unchanged (Kyle / prior PRs)

- **Hard email cap 10 until further notice (Kyle 2026-09-11)** — replaces “cap = N bill attempts / walk past noise” (PR #19). Noise is Excel `Skipped` with Outlook **`AI Skipped`** (never `AI HOLD`) and **consumes** the 10-email touch cap.
- **Skip already-flagged** — `Entered in AI` / `AI HOLD` / `Entered with issues` / `AI Skipped` / `flag.flagStatus=flagged` are walked past without touching and do not consume the cap.
- One Mail.Send per run after the final sheet only.
- No auto-pay. No auto-close batch. Payments human-gated.
- Shawn McKibben comments on price-does-not-match.
- Success = finished bill only (now also: Treyce would not rework it).

Pause further dry runs until Kyle reviews this V1.2 PR.

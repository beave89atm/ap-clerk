# QUALITY V1.2

Treyce’s 2026-09-10 notes on the 8/16 dry-10 sheet, plus Kyle’s never-repeat and
**Treyce-load / fix-before-complete** rules. Next live FIFO continues AFTER the
**2026-09-15** weekday cursor (`2026-08-19T20:12:03Z` / batch **711**).

**Hard email cap 10 until further notice (Kyle 2026-09-11).** Cap = mailbox
messages *touched* (Success, HOLD, Incomplete, Fail, Skipped/noise). Stop after
10 emails. Do **not** walk past noise to fill N bill attempts. Bill-attempt mode
is suspended until Kyle lifts this. Weekday/daily `--limit 30` is hard-clamped
to 10. Still note each of those ≤10 on the sheet with Why.

**Skip already-flagged (Kyle 2026-09-11).** Leave alone — do not reprocess, do
not re-stamp categories — if the message already has Outlook `Entered in AI`,
`AI HOLD`, `Entered with issues`, `AI Skipped 2`, leftover `AI Skipped`, or Graph
`flag.flagStatus=flagged`. Those messages do **not** consume the 10-email
touch cap. Only unflagged / uncategorized (by those AP markers) messages count.

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

## Outlook categories (exactly four)

| Result | Outlook |
| --- | --- |
| **Success** (finished; Treyce would not rework) | `Entered in AI` |
| Header + PDF entered but bill cannot be finished (price-does-not-match, qty HOLD, Incomplete finish) | `Entered with issues` |
| Real bill unprocessable **without** a header (parse-error / no-pdf, auto-pay, pdf-behind-link, Fail) | `AI HOLD` |
| Noise (not-a-bill, statement, CHECK STOP notice, payment, POD, duplicate) | **`AI Skipped 2`** — sheet Result stays `Skipped` with Why |

**Kyle action:** Treyce created Outlook category **`AI Skipped 2`** (space before 2) on `accountspayable@`. Code POSTs that exact name and PATCHes the exact string. Do **not** stamp `AI Skipped`. If Graph cannot find/create `AI Skipped 2`, Why is `outlook-category-missing: AI Skipped 2` and the sheet row stays `Skipped`. Historical `AI Skipped` stamps remain on old mail and are treated as already-flagged (no re-stamp).

Never a fifth going-forward process-category name. Leftover `AI Skipped` on old mail is already-flagged only — do not stamp it again. Never two process markers on the same message. Do **not** leave noise uncategorized. Do **not** use `AI HOLD` for noise.

**Account Statements (Kyle 2026-09-14).** Account Statements / statements-of-account / past-due invoice lists are not invoices. Skip — do nothing (no header, no Select Receipts, no Success). Outlook `AI Skipped 2`. Julie Hencke 2026-08-18 `Past Due Invoices` is the same skip. Do not void leftover KIMCO **9985**.

**Invoice-from is not a statement (Kyle 2026-09-15).** Subject `Invoice` / `INV` / `bill` (like `Invoice from Greentree Packaging & Lumber`) is a bill. Preview/body `account statement` tokens must not AI Skipped 2 it. A real invoice PDF attachment is never statement. If unsure, inspect the PDF first; prefer enter (header+attach) or bill HOLD over Skip when an invoice PDF exists. **Honest miss:** weekday 2026-09-15 live-10 (batch **711**) wrongly Skipped Greentree as `Skipped (bill-vs-noise): statement` — Outlook `AI Skipped 2`, `no-pdf-on-vm`, empty invoice # — before reading the attached invoice. Do not invent Success for that row. Do not void unrelated rows.

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
   TXFT4100079 qty 36 vs invoice 35). **Check every invoice line.** Select
   every line that matches. Do not stop after one unmatched line. Do not
   fail-close the whole bill to `no receipts after second pass` when some
   lines have matching PO receipts (3P 9988–9991). Partial select is
   required: leftover lines stay HOLD / Incomplete / Entered with issues
   with Why naming selected vs unmatched.
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
| **NOTE-09** AQPC link-download PDF | Silent not-a-bill | Unauth GET, then guest browser click-through (no Intuit login); PDF → header+attach; true failure after guest View/Download invoice → `pdf-behind-link` HOLD (Why says guest browser was tried); never Success | `test_note09_aqpc_pdf_behind_link` |
| **NOTE-10** Gas & Supply Misc vs CHECK STOP | Blanket CHECK STOP | Invoice pages → Type 4 `Shop Supplies - G&S`; notice skip; ambiguous HOLD; never Success | `test_note10_gas_supply_misc_vs_check_stop` |
| **NOTE-11** Nova Alloys 258145 / From Erica Barrett (8/18) | Vendor=`Erica Barrett`; HOLD preflight-parse (`invoice #` tagged `subject`); Attach `no-pdf-on-vm` though PDF was on disk | PDF-is-truth: Vendor=Nova Alloys; same # on subject is OK; create header+attach; Why describes THIS bill (no MSC/McQueary); only HOLD no-pdf if file missing | `test_never_repeat_nova_258145` |
| **NOTE-12** MSC 70762501 / KIMCO 9967 posted as RMP (8/18) | Parsed MSC Industrial Supply; Result Success; live GET `1320-RMP INDUSTRIAL SUPPLY` type 4 | MSC ≠ RMP (distinctive tokens); alias 128 over fuzzy seed; GET posted vendor must match or HOLD `vendor-mismatch`; never Success | `test_never_repeat_msc_70762501_not_rmp` |
| **NOTE-13** Crosslink 27943 / 27944 / 27946 (8/18) | HOLD preflight-parse (`invoice #` tagged `filename`); Attach `no-pdf-on-vm` though `invoice-27943.pdf` was on disk | PDF-is-truth: filename # + PDF on disk is not a parse HOLD; create header+attach; Why describes THIS Crosslink bill (no MSC/McQueary); no-pdf-on-vm forbidden when file exists | `test_never_repeat_crosslink_27943` |
| **NOTE-14** Fastenal TXFT4100079 / PO 58692 / KIMCO 9968 (Kyle 2026-09-11) | Select Receipts qty **36** (first open on PO) vs invoice **35**; Why said “not first qty”; Shipping & Handling 63.98 Excel-only, never Additional Charge | Verify qty/cost; pick 35 not 36; never first-open / second-open-on-po Success when open receipts differ; post F-Fees & Surcharges 63.98 before Success or Incomplete / Entered with issues | `test_never_repeat_fastenal_txft4100079` |
| **NOTE-15** EMJ Z250725432 / PO 58913 / KIMCO 9969 (Kyle 2026-09-11) | Two invoice lines; `lines:[]` then one PO receipt; Success; skipped line not on sheet; random-length gap not PPV | Parse both lines; Select Receipts per line; unmatched → not Success + Why names the skipped line; small length variance is PPV (≤10% / ≤$100), not Fees; prepaid/ship-date null amount is not a fee | `test_never_repeat_emj_z250725432_two_lines` |
| **NOTE-16** Gas `billing01_A3050_c.pdf` / 0040370068 / KIMCO 9970 (Kyle 2026-09-11) | 6 invoices collapsed to one # + multi-PO Incomplete; amount 322 was before tax | Split to 6 bills; after-tax Amount Due (never Subtotal/Merchandise); page-range PDF per invoice when feasible else full pack + Why `multi-invoice-pdf page X–Y of N`; email still 1 touch; never Success/Incomplete a collapsed pack or a pre-tax amount | `test_never_repeat_gas_multi_invoice_pdf` / `test_never_repeat_gas_after_tax_amount` |
| **NOTE-17** Insight 1809 already entered (Kyle 2026-09-11) | HOLD preflight-parse MSC/McQueary + `no-pdf-on-vm`; never said already-entered | Duplicate check before parse-HOLD; HOLD `already-entered` names vendor / # / existing KIMCO id(s); no McQueary; no `no-pdf-on-vm` when PDF is on disk | `test_never_repeat_insight_1809_already_entered` |
| **NOTE-18** Outlook `AI Skipped 2` for noise (Kyle 2026-09-11 / Treyce 2026-09-14) | Noise was sheet-only; no Outlook category; already-flagged ignored `AI Skipped` | Stamp exact `AI Skipped 2` (never `AI HOLD`, never `AI Skipped`); Why `outlook-category-missing: AI Skipped 2` if Graph cannot apply; already-flagged includes `AI Skipped 2` and leftover `AI Skipped` | `test_never_repeat_ai_skipped_noise` |
| **NOTE-19** 3P / Rachel Bailey INV# 142041–142044 multi-PO (8/18) | Skipped not-a-bill | Invoice, not noise; Invoice_Type 3 (not Misc 4); header PO blank; Select Receipts per PO **by invoice line part + PO + qty** (CPL # is a secondary slip hint only, never a gate); sheet lists POs + selected receipts; unmatched PO named on Why; Outlook bill categories, never `AI Skipped 2` | `test_never_repeat_3p_rachel_bailey_not_noise` / `test_3p_multi_po_select_receipts` |
| **NOTE-23** 3P 142041–142044 / KIMCO 9988–9991 (live 9/14 batch 708) | Header + PDF entered; **zero** Select Receipts; Why `no receipts after second pass` even though open receipts existed on those PO lines. Do not invent a $0.02 PPV — invoice amounts add cleanly; PPV is none unless a real unit-price gap qualifies. | Match each invoice line → open receipts on that line's listed PO by **part / qty / PO** (CPL not required). Check every line; select every match. Price gaps do **not** skip receipts: qualifying variance (≤10% of invoice total and ≤$100 bill PPV) posts Additional Charge Purchase Price Variance; over threshold → HOLD price-does-not-match + `@Shawn McKibben` and still select other good lines. 142043 receipt qty 6 / invoice 4 → select qty 4 if the API allows. Fees ≠ PPV. Partial select → Entered with issues / Incomplete; never Success if a human must fix price/qty; never zero-receipt HOLD when Notes-style matches exist. | `test_never_repeat_3p_select_receipts_cpl` / `test_never_repeat_3p_notes_142041_142044` |
| **NOTE-20** Eastern Metal 818600 / 818601 (8/18) | Skipped not-a-bill | `Invoice` + Eastern Metal / EASTERN METAL SUPPLY (alias 64) is a bill; Invoice/INV subject **or** PDF invoice attached is never not-a-bill; enter or HOLD | `test_never_repeat_eastern_metal_818600_not_noise` |
| **NOTE-21** AQPC 10917 / 10918 payment-request link (8/18; live 9/15 Intuit) | Skipped not-a-bill + `no-pdf-on-vm` | **Link download is mandatory:** extract https payment-request / Intuit URL, unauth GET, then guest browser if auth/bot-walled or intermediate HTML. Click View/Download invoice with no Intuit login. PDF → header + attach. True failure after guest browser → HOLD `pdf-behind-link` names vendor / # / host and that guest browser was tried — never Skipped, never “set `AP_CLERK_INTUIT_STORAGE_STATE`” | `test_never_repeat_aqpc_10917_link_download` |
| **NOTE-22** KIMCO vendor + invoice never skip (Kyle) | Listed vendors with Invoice/INV subjects were Skipped | KIMCO From/subject + invoice (PDF, link-PDF, or Invoice/INV subject) → enter or HOLD with real Why; never Skipped / `AI Skipped 2`. `AI Skipped 2` only for true non-vendor noise | `test_never_repeat_kimco_vendor_invoice_never_skip` |
| **NOTE-24** Leeco Account Statement 2026-08-18 / leftover KIMCO **9985**; Julie Hencke `Past Due Invoices` (live 9/14 batch 708) | Leeco entered as a bill (filename 1058256); listed 617228 / 617448 / 619920 / 619921. Word `Invoices` on a past-due list must not flip it to a bill. | Account Statement / statement-of-account / past-due invoice list (subject or PDF body) → `Skipped` + Outlook `AI Skipped 2`. No header, no Select Receipts, no Success. Do not void 9985 | `test_never_repeat_leeco_account_statement` / `test_never_repeat_julie_hencke_past_due_invoices` |
| **NOTE-25** Legacy Wire packing slip 114745 + PS-INV103979 / KIMCO **9995** + PS-INV103980 / KIMCO **9996** (live 9/15 batch 711) | **Honest miss:** matcher over-held on rolled qty / cost uniqueness instead of line matches. 114745 HOLD parse-error from `Receipt_114745.pdf` (signed packing slip, not an invoice). 103979 HOLD qty 77 from `77"` TUBE. 103980 HOLD “merchandise cost does not uniquely align” though every invoice line matched; freight never Fees; Select Receipts left `held-unfinished`. | Packing slip / POD / signed delivery receipt → disregard (no HOLD parse-error, no invented #). Invoice # exactly as on that PDF (`PS-INV*`). Select every line that matches part+qty+PO even if other open receipts exist on the PO. Do not HOLD cost-uniquely-align when line matches are clear. Freight → Additional Charge Fees. Still no first-open guess when lines do **not** match. Do not rewrite 9995/9996 | `test_never_repeat_legacy_receipt_114745_not_invoice` / `test_never_repeat_legacy_ps_inv103979_and_103980` |
| **NOTE-26** Greentree Packaging & Lumber `Invoice from Greentree Packaging & Lumber` (live 9/15 batch 711) | **Honest miss / false statement skip:** sheet Why `Skipped (bill-vs-noise): statement`; Outlook `AI Skipped 2`; Attach `no-pdf-on-vm`; empty invoice #. Classifier treated preview/body `account statement` as noise **before** inspecting the attached invoice PDF (PDF-is-truth violated). Email consumed the 10-cap without entering the bill. | Subject Invoice/INV/bill hint (`Invoice from …`) **or** a real invoice PDF → never statement / never `AI Skipped 2`. If unsure, download/inspect the PDF first; prefer enter (header+attach) or bill HOLD over Skip. Leeco Account Statement / Julie Hencke `Past Due Invoices` still skip. Do not invent Success. Do not void unrelated rows | `test_never_repeat_greentree_invoice_from_not_statement` |

### Deferred (failing-safe stubs — not silent skips)

| Note | Limitation | Gate if we cannot finish |
| --- | --- | --- |
| NOTE-10 | Shared-total-only Gas packs (no per-invoice Amount Due) | HOLD `preflight-parse` when `gas_misc_ambiguous` |

### AQPC / Intuit payment-request links (guest — no login)

AQPC emails (`10917` / `10918` / `10920` / `10921`) link
`links.notification.intuit.com`. A human opens that URL and sees the
invoice **without** signing in to Intuit/QuickBooks. The runner must do
the same. Kannon does not have (and does not need) a vendor-portal login.
`AP_CLERK_INTUIT_STORAGE_STATE` / MFA / a saved Intuit session are **not**
blockers and are **not** the standing fix.

Cheap unauthenticated GET still runs first. An auth/bot wall or
intermediate HTML escalates to Playwright (system Chrome when present)
as a **guest**: open the `links.notification.intuit.com` View-details
click (not the `sale/viewed` tracking pixel), then click
View/Download invoice. No storage state is loaded for success.

**Env (names only — never commit the files or values):**

| Name | Purpose |
| --- | --- |
| `AP_CLERK_BROWSER_PDF` | Set `0` / `false` / `no` to skip the browser escalate. Default: on. |
| `AP_CLERK_BROWSER_PDF_TIMEOUT` | Browser wait seconds (default 45). |
| `AP_CLERK_INTUIT_STORAGE_STATE` | Optional Playwright `storage_state` JSON for **other** portals later. Not required for AQPC. |
| `AP_CLERK_INTUIT_COOKIE_JAR` | Optional cookie JSON for other portals later. Not required for AQPC. |

Daily CLI prints present/absent for these names only.

If the guest browser still fails, the bill is HOLD `pdf-behind-link`
with Why that says guest browser was tried and what failed. Next action
is retry the guest View/Download invoice click — **not** “set
`AP_CLERK_INTUIT_STORAGE_STATE`”. Never `AI Skipped`. Do not invent
Success. Do not void prior HOLDs until one live AQPC invoice is proven
with a real guest click-through.

## Unchanged (Kyle / prior PRs)

- **Hard email cap 10 until further notice (Kyle 2026-09-11)** — replaces “cap = N bill attempts / walk past noise” (PR #19). Noise is Excel `Skipped` with Outlook **`AI Skipped 2`** (never `AI HOLD`) and **consumes** the 10-email touch cap.
- **Skip already-flagged** — `Entered in AI` / `AI HOLD` / `Entered with issues` / `AI Skipped 2` / leftover `AI Skipped` / `flag.flagStatus=flagged` are walked past without touching and do not consume the cap.
- One Mail.Send per run after the final sheet only.
- No auto-pay. No auto-close batch. Payments human-gated.
- Shawn McKibben comments on price-does-not-match.
- Success = finished bill only (now also: Treyce would not rework it).

## Monday 2026-09-14 2:00am America/Chicago — live 10

Scheduled job: `daily --live --limit 30` (hard-clamped to **10 emails**). No
mailbox/KIMCO run from this PR before that job. Goal: ~90% without the false
Skip / false Success class of errors.

| # | Basic | Note / test |
| --- | --- | --- |
| 1 | Never skip KIMCO vendor invoices | NOTE-22 `test_never_repeat_kimco_vendor_invoice_never_skip` |
| 2 | PDF-is-truth + accurate Why | NOTE-11 / 13 + Why-must-be-true |
| 3 | Qty/cost receipt verify + post fees | NOTE-14 Fastenal TXFT4100079 |
| 4 | All invoice lines or explicit skip note | NOTE-15 EMJ Z250725432 |
| 5 | Multi-invoice PDF split + after-tax total | NOTE-16 Gas 0040370068 |
| 6 | 3P multi-PO Select Receipts | NOTE-19 |
| 7 | AQPC link download | NOTE-21 |
| 8 | Eastern Metal not noise | NOTE-20 |
| 9 | Duplicate / already-entered Why | NOTE-17 Insight 1809 |
| 10 | `AI Skipped 2` for true noise only | NOTE-18 |

### Blockers / failing-safe (not silent Skip or Success)

- **Outlook master categories** `Entered with issues` and **`AI Skipped 2`**
  (Treyce created `AI Skipped 2` on `accountspayable@`). Graph masterCategories
  POST is often 403. Code still PATCHes the exact strings. Missing `AI Skipped 2`
  → Why `outlook-category-missing: AI Skipped 2`; sheet stays Skipped. Old
  `AI Skipped` stamps are left in place.
- **AQPC Intuit click-through** (NOTE-09 / NOTE-21): unauth GET then guest
  browser (no Intuit login). HOLD `pdf-behind-link` only after the guest
  View/Download invoice click fails. Why names vendor / # / host and that
  guest browser was tried. Do not ask Kyle to save an Intuit session.
  Never Skipped.
- **Gas shared-total-only packs** (NOTE-10): HOLD `preflight-parse` /
  `gas_misc_ambiguous` — never invent per-invoice amounts.
- **Fastenal** has no confirmed `Vendor.id` alias row (never-skip uses the name
  token; header vendor still comes from PO / samples). Do not invent an id.
- **3P** live vendor is `999-3P INDUSTRIES` (GET 9988–9991). Do not invent a
  different id. Receipt `part` fields are often `PO58766-01` (PO line names),
  not `1007044-1` — match invoice part/qty to open receipts on that PO.

## 3P Select Receipts (Kyle 2026-09-14, NOTE-23)

Primary match path is **invoice line → open receipts on the listed PO(s)**
by part / PO line / per-line qty (existing Select Receipts rules).

- Subject `CPL # 76659, 76664, …` numbers are **packing-slip hints only**.
  Never require CPL to find receipts. Never HOLD no-receipts because CPL
  matching failed when PO-line receipts exist for the invoice parts.
- Do **not** use the invoice-total qty/cost as the per-line gate. A 5-line
  3P bill (qty 1+6+9+47+36) must match each line's qty on that line's PO,
  not qty 99 against every receipt.
- Multi-line does **not** disable the open-on-PO fallback. Apply it
  **per PO** (one unmatched line on PO 58766 + that PO's open receipts).
- Partial Select Receipts is required when any line matches. Result is
  Success only if every merchandise line is selected; otherwise Entered
  with issues (or Incomplete) — not a zero-receipt HOLD.

Live 9/14 afternoon (`API Agent - 9/14/26` batch **708**): KIMCO **9988–
9991** (invoices 142041–142044) were HOLDed with zero receipts selected.
Root cause was the matcher (invoice-total qty/cost + multi-line blocking
open-on-PO), not missing receipts. Do not create duplicate headers.

Evening GET of the same four ids (do not invent Success): someone already
posted Notes-style **partial** Select Receipts — 9988 lines 2/4/5 (the
MUST-pull lines); 9989 both lines (line 2 at unit 75 vs invoice 83.02);
9990 qty **4** on PO 58862; 9991 lines 1/2/4. Price-wrong / no-receipt
leftovers remain. Additional Charge PPV count is 0. Invoice totals add
cleanly; do not invent a $0.02 PPV. This PR is so the clerk does that
select itself next time.

## Legacy Wire line receipts + packing slips (Kyle 2026-09-15, NOTE-25)

Prior matcher over-held on **rolled qty** and **PO-pool cost uniqueness**
instead of invoice **line** matches.

- `Receipt_114745.pdf` / `Receipt_*` / POD / signed packing slip is **not**
  an invoice. Classify and disregard. Never invent `114745` / `103979` /
  `120911` / `121051` from the filename. Never `AI HOLD` a slip as a bill.
- Multi-invoice email or PDF is still N bill rows. An email with
  `PS-INV103979` + a packing slip processes **only** the invoice.
- Invoice # is exactly as printed (`PS-INV103979`), never stripped.
- Select Receipts **line-by-line** (part + qty + PO). Extra open receipts
  on the same PO do **not** HOLD “merchandise cost does not uniquely align”
  when those line matches are clear (PS-INV103980 / 9996).
- `77"` in a description is an inch dimension, not qty 77 (PS-INV103979 / 9995).
- Freight / shipping / delivery → Additional Charge **Fees and surcharges**.
  It does not block Select Receipts. Post Fees when parsed; do not leave
  matched Select Receipts `held-unfinished`.
- Still no first-open / second-open-on-po guess when lines do **not** match.
- Live **9995** / **9996** were already worked by Kyle (do not rewrite;
  do not invent Success).

Pause further ad-hoc dry runs. Weekday 2026-09-15 live-10 used this stack
(`daily --live --limit 10` from the 9/14 afternoon cursor). **0 Success** —
AQPC Intuit links auth-walled on unauthenticated GET (guest browser
click-through was not implemented on that run); Legacy 9995/9996 headers
need Treyce Select Receipts; Crosslink 27321/27319/27419 were
already-entered. **Greentree Packaging & Lumber was a false statement
skip** (NOTE-26): subject `Invoice from Greentree Packaging & Lumber` +
attached invoice, sheet `Skipped (bill-vs-noise): statement`, Outlook
`AI Skipped 2`, `no-pdf-on-vm`. Do not invent Success or recreate those
headers. Next weekday continues AFTER `2026-08-19T20:12:03Z`. AQPC
10917/10918/10920/10921-class mail should guest-click through with no
Intuit session; HOLD `pdf-behind-link` only after that guest browser
attempt fails — never Skipped.

### One-invoice AQPC guest proof (2026-09-15, after Kyle posted 10917)

Touched **only** mailbox `New payment request from AMERICAN QUALITY POWDER
COATING - invoice 10917` (the 9/15 `pdf-behind-link` HOLD). Guest Playwright
(no `AP_CLERK_INTUIT_STORAGE_STATE`) downloaded a 23824-byte `%PDF-1.4`.
Live GET: invoice **10917** is already **KIMCO 10003** — vendor 22
`1020-AMERICAN QUALITY POWDERCOATING`, PO 58944, Type 3, amount 275.00,
PDF attached, 1 receipt line qty 1 @ 275 (receipt 23186). **Posted**
2026-09-15 by Kyle Cleaver from batch `9_15_26-KC` (713). This proof did
**not** create a second header. Sheet Result is HOLD `already-entered`.
Outlook restamped **Entered in AI**. No Treyce Mail.Send. Do not void
10003. Do not invent Success on this proof row.

### Next AQPC guest enter (2026-09-15, after Kyle posted 10918/10920/10921)

Live GET first: **10918 = 10002**, **10920 = 10005**, **10921 = 10004**
(vendor 22, Type 3, PDF + receipts, posted from `9_15_26-KC` 713) — same
already-entered class as 10917/10003. Did **not** touch those emails or
create second headers.

Touched **one** unflagged payment-request:
`New payment request from AMERICAN QUALITY POWDER COATING - invoice 11002`.
Guest Playwright (no `AP_CLERK_INTUIT_STORAGE_STATE`) downloaded a
23482-byte `%PDF-1.4` from the Intuit **View details** click (not
`sale/viewed`). PDF-is-truth: invoice **11002**, date 9/14/26, PO **59172**,
amount **300.00**, line `AMT-5003558` qty **100** @ $3.00 (the leading
`1.` is the QBO line number, not qty).

Created **KIMCO 10007** on batch `API Agent - 9/15/26` (**711**). 711 was
Status 1 / Unposted_Count 0; a second same-name batch create returned 400;
reopen PUT Status 0 returned 405; header create on 711 still returned 200.
Live GET: vendor 22, Type 3, PDF attached, Select Receipts **24104** qty
100 @ 3.00, no fees. Outlook **Entered in AI**. No Treyce Mail.Send.
Sheet: `runs/AP-run-2026-09-15-aqpc-next.xlsx`. Do not void 10007.
Do not invent Success for 10918/10920/10921.

### Plus-4 AQPC on batch 711 (2026-09-15)

Touched **only** four unflagged AQPC payment-request emails. Graph has no
11006+. Chosen: **11003**, **11004** (later of two emails), **11005**, and
**10999** (next newest unflagged AQPC not already on KIMCO). Did not walk
unrelated vendors. Did not recreate 10917/10918/10920/10921/11002.

Guest Playwright View-details clicks (no Intuit login / no storage-state)
downloaded `%PDF-1.4` files. PDF-is-truth qtys are not QBO line numbers.
Reused batch `API Agent - 9/15/26` (**711**, Status 0). No Treyce Mail.Send.
Sheet: `runs/AP-run-2026-09-15-aqpc-plus4.xlsx`. `invent=false`.

| Invoice | KIMCO | Result | Receipts | Amount | PO |
| --- | --- | --- | --- | --- | --- |
| 10999 | **10008** | **Success** | 23978 qty 12 @ 10.00 | 120.00 | 59160 |
| 11003 | **10009** | HOLD price-does-not-match | 24103 qty 2 @ 0.777 | PDF 10.00 / posted 1.55 | 59083 |
| 11004 | **10010** | HOLD qty-does-not-match | partial 24106/07/08/11 ($2,400); leftover 04/05 | PDF 2600.00 | 59165 |
| 11005 | **10011** | **Success** | 24105 qty 5 @ 30.00 | 150.00 | 59118 |

**2 Success / 4 attempted.** First-pass named-po left the single AQPC line
unmatched even after Select Receipts (false finish HOLD on 10999/11005);
live GET matched the PDF, so those two were restamped **Entered in AI**.
11003: PO/receipt $0.777 vs invoice $5.00 (84.4%) — `@Shawn McKibben`,
Entered with issues. 11004: invoice line 4 qty 15 vs PO59165-04 qty 5 and
line 5 qty 5 vs PO59165-05 qty 15 (same $10 unit; qtys look swapped);
partial Select Receipts on the four matching lines; Entered with issues.
Do not void 10008–10011. Do not invent Success for 11003/11004.

# QUALITY V1.2

Treyce’s 2026-09-10 notes on the 8/16 dry-10 sheet, plus Kyle’s never-repeat and
**Treyce-load / fix-before-complete** rules. Next live FIFO continues AFTER the
**2026-09-16** weekday cursor (`2026-08-20T04:57:35Z` / batch **715**).

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

**NOTE-28 AQPC too-old (Kyle 2026-09-16).** AQPC invoice date **before
2026-08-01 → skip / do not create a header.** After Aug/Sep AQPC
payment-requests are exhausted, **stop** — do not walk older mail into
KIMCO. Kyle reversed 10040–10046 (10696 / 10523 / 10381 / 9502 / 9498 /
9352 / 9343) as too-old. Do not re-enter them. Same-cost leftover 10956 /
10021 stays (NOTE-27).

**NOTE-29 over-PPV do not lock receipts (Kyle 2026-09-16).** If leftover
vs invoice line is outside the PPV gate (≤10% of invoice total **and**
bill PPV ≤$100), **do not Select Receipts** for that line (selecting
locks the receipt; Shawn cannot unreceive / fix PO / re-receive). Whole
bill over-gate → select **zero**. Still header + PDF. HOLD
`price-does-not-match`. Live released 10009 / 24103 and 10013 / 23967.

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
   **Kyle 2026-09-16 (NOTE-29):** if a leftover is over that gate, **do not
   Select Receipts** for that line (selecting locks the receipt; Shawn
   cannot unreceive, fix the PO price, and re-receive). If the whole bill
   is over-gate, select **zero** receipts. Still create header + attach
   PDF. AQPC **11003 / 10009** and **10991 / 10013** are the class.
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
| **NOTE-23** 3P 142041–142044 / KIMCO 9988–9991 (live 9/14 batch 708) | Header + PDF entered; **zero** Select Receipts; Why `no receipts after second pass` even though open receipts existed on those PO lines. Do not invent a $0.02 PPV — invoice amounts add cleanly; PPV is none unless a real unit-price gap qualifies. | Match each invoice line → open receipts on that line's listed PO by **part / qty / PO** (CPL not required). Check every line; select every **in-gate** match. Qualifying variance (≤10% of invoice total and ≤$100 bill PPV) posts Additional Charge Purchase Price Variance. Over threshold → HOLD price-does-not-match + `@Shawn McKibben` and **do not Select** the over-PPV line (NOTE-29 / 11003 / 10991); still select other in-gate lines. 142043 receipt qty 6 / invoice 4 → select qty 4 if the API allows. Fees ≠ PPV. Partial select → Entered with issues / Incomplete; never Success if a human must fix price/qty; never zero-receipt HOLD when Notes-style matches exist. | `test_never_repeat_3p_select_receipts_cpl` / `test_never_repeat_3p_notes_142041_142044` |
| **NOTE-20** Eastern Metal 818600 / 818601 (8/18) | Skipped not-a-bill | `Invoice` + Eastern Metal / EASTERN METAL SUPPLY (alias 64) is a bill; Invoice/INV subject **or** PDF invoice attached is never not-a-bill; enter or HOLD | `test_never_repeat_eastern_metal_818600_not_noise` |
| **NOTE-21** AQPC 10917 / 10918 payment-request link (8/18; live 9/15 Intuit) | Skipped not-a-bill + `no-pdf-on-vm` | **Link download is mandatory:** extract https payment-request / Intuit URL, unauth GET, then guest browser if auth/bot-walled or intermediate HTML. Click View/Download invoice with no Intuit login. PDF → header + attach. True failure after guest browser → HOLD `pdf-behind-link` names vendor / # / host and that guest browser was tried — never Skipped, never “set `AP_CLERK_INTUIT_STORAGE_STATE`” | `test_never_repeat_aqpc_10917_link_download` |
| **NOTE-22** KIMCO vendor + invoice never skip (Kyle) | Listed vendors with Invoice/INV subjects were Skipped | KIMCO From/subject + invoice (PDF, link-PDF, or Invoice/INV subject) → enter or HOLD with real Why; never Skipped / `AI Skipped 2`. `AI Skipped 2` only for true non-vendor noise | `test_never_repeat_kimco_vendor_invoice_never_skip` |
| **NOTE-24** Leeco Account Statement 2026-08-18 / leftover KIMCO **9985**; Julie Hencke `Past Due Invoices` (live 9/14 batch 708) | Leeco entered as a bill (filename 1058256); listed 617228 / 617448 / 619920 / 619921. Word `Invoices` on a past-due list must not flip it to a bill. | Account Statement / statement-of-account / past-due invoice list (subject or PDF body) → `Skipped` + Outlook `AI Skipped 2`. No header, no Select Receipts, no Success. Do not void 9985 | `test_never_repeat_leeco_account_statement` / `test_never_repeat_julie_hencke_past_due_invoices` |
| **NOTE-25** Legacy Wire packing slip 114745 + PS-INV103979 / KIMCO **9995** + PS-INV103980 / KIMCO **9996** (live 9/15 batch 711) | **Honest miss:** matcher over-held on rolled qty / cost uniqueness instead of line matches. 114745 HOLD parse-error from `Receipt_114745.pdf` (signed packing slip, not an invoice). 103979 HOLD qty 77 from `77"` TUBE. 103980 HOLD “merchandise cost does not uniquely align” though every invoice line matched; freight never Fees; Select Receipts left `held-unfinished`. | Packing slip / POD / signed delivery receipt → disregard (no HOLD parse-error, no invented #). Invoice # exactly as on that PDF (`PS-INV*`). Select every line that matches part+qty+PO even if other open receipts exist on the PO. Do not HOLD cost-uniquely-align when line matches are clear. Freight → Additional Charge Fees. Still no first-open guess when lines do **not** match. Do not rewrite 9995/9996 | `test_never_repeat_legacy_receipt_114745_not_invoice` / `test_never_repeat_legacy_ps_inv103979_and_103980` |
| **NOTE-26** Greentree Packaging & Lumber `Invoice from Greentree Packaging & Lumber` (live 9/15 batch 711) | **Honest miss / false statement skip:** sheet Why `Skipped (bill-vs-noise): statement`; Outlook `AI Skipped 2`; Attach `no-pdf-on-vm`; empty invoice #. Classifier treated preview/body `account statement` as noise **before** inspecting the attached invoice PDF (PDF-is-truth violated). Email consumed the 10-cap without entering the bill. | Subject Invoice/INV/bill hint (`Invoice from …`) **or** a real invoice PDF → never statement / never `AI Skipped 2`. If unsure, download/inspect the PDF first; prefer enter (header+attach) or bill HOLD over Skip. Leeco Account Statement / Julie Hencke `Past Due Invoices` still skip. Do not invent Success. Do not void unrelated rows | `test_never_repeat_greentree_invoice_from_not_statement` |
| **NOTE-27** AQPC 10956 / KIMCO **10021** / PO 59016 / receipt **23517** (batch 711) | **Honest miss / false qty HOLD:** line 1 Rack 2@$200 selected 23516; line 2 plate invoice qty 6 @$50 = $300 vs leftover 23517 qty 2 @$150 = $300. Same cost, qty/unit inverted. Sheet stayed HOLD qty-does-not-match (posted $400 vs PDF $700). | When leftover receipt extended cost uniquely matches the invoice line total, Select Receipts even if qty and unit are inverted (same class as 11004 qty+unit swap). Do not PPV. Do not alter receipt unit price. Do not HOLD qty-does-not-match when the dollars already match. Never invent receipts. Kyle 2026-09-16: close 10956 with 23516+23517 | `test_never_repeat_aqpc_10956_same_cost_inverted_qty_unit` |
| **NOTE-28** AQPC invoice date before **2026-08-01** / KIMCO **10040–10046** (batch 711) | Discovery walked older AQPC payment-requests (10696 Jun 2026 … 9343 Apr 2025) into headers 10040–10046. | **Skip / do not create header** when AQPC invoice date is before 2026-08-01. After Aug/Sep is exhausted, stop — do not walk older payment-requests into KIMCO. Kyle reverse 2026-09-15/16: void 10040–10046; clear Outlook process categories; do not re-enter; sheet Result=Voided (too-old). Never Success. Never invent receipts. | `test_never_repeat_aqpc_too_old_before_2026_08_01` |
| **NOTE-29** Over-PPV leftover must not be selected (AQPC **11003 / 10009** receipt 24103; **10991 / 10013** receipt 23967) | Headers created; over-PPV leftovers were still Select Receipts’d (~84% / $8.45 on 11003; ~25% / $49.75 on 10991). That locked the receipt so Shawn could not unreceive, fix the PO price, and re-receive. | If leftover vs invoice line is outside the PPV gate (≤10% of invoice total **and** bill PPV ≤$100), **do not Select Receipts** for that line. Whole bill over-gate → select **zero**. Still header + PDF. HOLD `price-does-not-match` + `@Shawn McKibben`. Outlook Entered with issues. Never Success. Kyle 2026-09-16: release 24103 / 23967 on 10009 / 10013. | `test_never_repeat_aqpc_over_ppv_does_not_select_receipts` |

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

Weekday 2026-09-16 live-10 used `daily --live --limit 10` from that
9/15 cursor on stacked tip `cursor/aqpc-plus5-batch711-25-307b`. Batch
**715** `API Agent - 9/16/26`. **0 Success** after reversing daily's
Type-4 Success on EMJ `Z250741432` / **10048** (PDF has 3 mill lines
totaling $4,222.58; parser `lines:[]`; live GET amount 0.00; no unique
receipt by qty+cost). Also Incomplete Priority 1 **10047** (fees
blocked-400), Fail McNichols `2559543` (vendor parsed as From email;
PO 58935 exists), HOLD Metal **10049** qty and O'Neal **10050**
price-does-not-match. Gas & Supply split but amounts not extracted —
no invented totals. Next weekday continues AFTER `2026-08-20T04:57:35Z`.

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
Do not void 10008–10011. Do not invent Success for 11003.

Kyle 2026-09-15 confirmed 11004 leftovers are a **line-order swap**: select
**24109** qty 5 @$10 and **24110** qty 15 @$10 by unique leftover qty+cost
(not PO suffix). Matcher pass `qty-cost-swap`. Do not touch 10009 (Shawn
price HOLD). Then enter 5 more unprocessed AQPC payment-requests on batch
**711**. Full-10 sheet: `runs/AP-run-2026-09-15-aqpc-batch711-10.xlsx`.
No Treyce Mail.Send.

### Plus-5 AQPC on batch 711 (2026-09-15)

Sibling **bc-655a55e9** / PR #39 already finished **10010** (24109+24110 by
qty+cost; $2,600 / six lines / Entered in AI). This run left 10010 as-is
(`already-selected`) and entered the next five unflagged payment-requests
(Graph has no 11006+): **10998, 10991, 10984, 10969, 10968**. Guest
Playwright, no Intuit login, reuse batch **711**. No Treyce Mail.Send.
`invent=false`.

| Invoice | KIMCO | Result | Receipts | Amount | PO |
| --- | --- | --- | --- | --- | --- |
| 11002 | **10007** | **Success** | 24104 100@3 | 300.00 | 59172 |
| 10999 | **10008** | **Success** | 23978 12@10 | 120.00 | 59160 |
| 11003 | **10009** | HOLD price-does-not-match | 24103 2@0.777 | PDF 10.00 / posted 1.55 | 59083 |
| 11004 | **10010** | **Success** | 24106–24111 incl. swapped 24109/24110 | 2600.00 | 59165 |
| 11005 | **10011** | **Success** | 24105 5@30 | 150.00 | 59118 |
| 10998 | **10012** | **Success** | 23979–23982 qty 6@10 | 240.00 | 59158 |
| 10991 | **10013** | HOLD price-does-not-match | 23967 199@0.75 | PDF 199.00 / posted 149.25 | 59148 |
| 10984 | **10014** | **Success** | 23897 1@5 | 5.00 | 59098 |
| 10969 | **10015** | **Success** | 23677 3@45 | 135.00 | 59079 |
| 10968 | **10016** | **Success** | 23682 1@10 | 10.00 | 59080 |

10998 first pass HOLD: `PO59158-01` was not line 1, so four identical qty-6
leftovers looked unmatched. Suffix parse + live PUT of 23979–23982; GET
$240. 10991: do not invent Success — 25% / $49.75 over Kyle’s PPV cap;
@Shawn; Entered with issues. 10009 left for Shawn. Do not void 10007–10016.

### Another plus-5 AQPC on batch 711 (2026-09-15)

Kyle: run another 5 AQPC only; add to batch **711**; update the sheet to
**15 rows** (prior 10 + 5 new). Do not recreate 10007–10016
(11002 / 10999 / 11003 HOLD / 11004 / 11005 / 10998 / 10991 HOLD / 10984 /
10969 / 10968). Skip Kyle-entered 10917/10918/10920/10921. Guest Intuit
View-details click (not `sale/viewed`). No Intuit login. No Treyce Mail.Send.
`invent=false`. Sheet: `runs/AP-run-2026-09-15-aqpc-batch711-15.xlsx`
(also `runs/AP-run-2026-09-15-aqpc-batch711.xlsx`).

Chosen next unflagged payment-requests (Graph still has no 11006+):
**10967, 10964, 10962, 10958, 10956**. Guest Playwright View-details
(no Intuit login / no storage-state). Reused batch **711** (Status 0,
Unposted_Count 15 after this enter). No Treyce Mail.Send. `invent=false`.

| Invoice | KIMCO | Result | Receipts | Amount | PO |
| --- | --- | --- | --- | --- | --- |
| 11002 | **10007** | **Success** | 24104 100@3 | 300.00 | 59172 |
| 10999 | **10008** | **Success** | 23978 12@10 | 120.00 | 59160 |
| 11003 | **10009** | HOLD price-does-not-match | 24103 2@0.777 | PDF 10.00 / posted 1.55 | 59083 |
| 11004 | **10010** | **Success** | 24106–24111 incl. swapped 24109/24110 | 2600.00 | 59165 |
| 11005 | **10011** | **Success** | 24105 5@30 | 150.00 | 59118 |
| 10998 | **10012** | **Success** | 23979–23982 qty 6@10 | 240.00 | 59158 |
| 10991 | **10013** | HOLD price-does-not-match | 23967 199@0.75 | PDF 199.00 / posted 149.25 | 59148 |
| 10984 | **10014** | **Success** | 23897 1@5 | 5.00 | 59098 |
| 10969 | **10015** | **Success** | 23677 3@45 | 135.00 | 59079 |
| 10968 | **10016** | **Success** | 23682 1@10 | 10.00 | 59080 |
| 10967 | **10017** | **Success** | 23681 10@50 | 500.00 | 59075 |
| 10964 | **10018** | **Success** | 23683 2@75 | 150.00 | 59076 |
| 10962 | **10019** | **Success** | 23520 4@25 | 100.00 | 59042 |
| 10958 | **10020** | **Success** | 23519 4@65 | 260.00 | 59051 |
| 10956 | **10021** | HOLD qty-does-not-match | partial 23516 2@200 | PDF 700.00 / posted 400.00 | 59016 |

**New five: 4 Success / 1 HOLD.** Live GET of 10017–10020 matches each PDF
qty/unit/total. 10956 line 1 (Rack 2@$200) selected **23516**; line 2
Aluminum Plate invoice qty 6 @$50 vs PO59016-02 receipt **23517** qty 2
@$150 — buyer qty HOLD, Entered with issues. Do not invent Success for
10956. Do not void 10007–10021. 10009 / 10013 left for Shawn.

### Another plus-5 AQPC on batch 711 (20-row sheet, 2026-09-15)

Kyle: run another 5 AQPC only; add to batch **711**; update the sheet to
**20 rows** (prior 15 + 5 new). Do not recreate 10007–10021
(11002 / 10999 / 11003 HOLD / 11004 / 11005 / 10998 / 10991 HOLD / 10984 /
10969 / 10968 / 10967 / 10964 / 10962 / 10958 / 10956 HOLD). Skip
Kyle-entered 10917/10918/10920/10921. Guest Intuit View-details click
(not `sale/viewed`). No Intuit login. No Treyce Mail.Send. `invent=false`.
Sheet: `runs/AP-run-2026-09-15-aqpc-batch711-20.xlsx`
(also `runs/AP-run-2026-09-15-aqpc-batch711.xlsx`).

Chosen next unflagged payment-requests (Graph still has no 11006+;
10951 not in mailbox): **10955, 10954, 10953, 10952, 10950**. Guest
Playwright View-details (no Intuit login / no storage-state). Reused
batch **711**. No Treyce Mail.Send. `invent=false`.

| Invoice | KIMCO | Result | Receipts | Amount | PO |
| --- | --- | --- | --- | --- | --- |
| 11002 | **10007** | **Success** | 24104 100@3 | 300.00 | 59172 |
| 10999 | **10008** | **Success** | 23978 12@10 | 120.00 | 59160 |
| 11003 | **10009** | HOLD price-does-not-match | 24103 2@0.777 | PDF 10.00 / posted 1.55 | 59083 |
| 11004 | **10010** | **Success** | 24106–24111 incl. swapped 24109/24110 | 2600.00 | 59165 |
| 11005 | **10011** | **Success** | 24105 5@30 | 150.00 | 59118 |
| 10998 | **10012** | **Success** | 23979–23982 qty 6@10 | 240.00 | 59158 |
| 10991 | **10013** | HOLD price-does-not-match | 23967 199@0.75 | PDF 199.00 / posted 149.25 | 59148 |
| 10984 | **10014** | **Success** | 23897 1@5 | 5.00 | 59098 |
| 10969 | **10015** | **Success** | 23677 3@45 | 135.00 | 59079 |
| 10968 | **10016** | **Success** | 23682 1@10 | 10.00 | 59080 |
| 10967 | **10017** | **Success** | 23681 10@50 | 500.00 | 59075 |
| 10964 | **10018** | **Success** | 23683 2@75 | 150.00 | 59076 |
| 10962 | **10019** | **Success** | 23520 4@25 | 100.00 | 59042 |
| 10958 | **10020** | **Success** | 23519 4@65 | 260.00 | 59051 |
| 10956 | **10021** | HOLD qty-does-not-match | partial 23516 2@200 | PDF 700.00 / posted 400.00 | 59016 |
| 10955 | **10022** | **Success** | 23518 5@65 | 325.00 | 59037 |
| 10954 | **10023** | **Success** | 23515 8@15 | 120.00 | 59024 |
| 10953 | **10024** | **Success** | 23514 3@65 | 195.00 | 59032 |
| 10952 | **10025** | **Success** | 23481–23486 (3@445, 3@5, 3@5, 9@10, 3@10, 3@25) | 1560.00 | 59025 |
| 10950 | **10026** | **Success** | 23457 2@50; 23895 1@50 | 150.00 | 59023 |

**New five: 5 Success / 0 HOLD.** Live GET of 10022–10026 matches each PDF
qty/unit/total. 10952/10950 first pass HOLD used rolled invoice qty and
list-view receipts with null qty/cost; record GET matched every PO-suffix
line, then Select Receipts. Outlook **Entered in AI**. Do not void
10007–10026. 10009 / 10013 / 10021 left for Shawn / buyer.

### Another plus-5 AQPC on batch 711 (25-row sheet, 2026-09-16)

Kyle: run another 5 AQPC only; add to batch **711**; update the sheet to
**25 rows** (prior 20 + 5 new). Do not recreate 10007–10026
(11002 / 10999 / 11003 HOLD / 11004 / 11005 / 10998 / 10991 HOLD / 10984 /
10969 / 10968 / 10967 / 10964 / 10962 / 10958 / 10956 HOLD / 10955 / 10954 /
10953 / 10952 / 10950). Skip Kyle-entered 10917/10918/10920/10921. Guest
Intuit View-details click (not `sale/viewed`). No Intuit login. No Treyce
Mail.Send. `invent=false`. Sheet: `runs/AP-run-2026-09-15-aqpc-batch711-25.xlsx`
(also `runs/AP-run-2026-09-15-aqpc-batch711.xlsx`). Same columns as batch711-20.

Chosen next unflagged payment-requests (Graph still has no 11006+;
10951 / 10949 / 10948 / 10947 not in mailbox): **10946, 10945, 10939,
10938, 10934**. Guest Playwright View-details (no Intuit login /
no storage-state). Reused batch **711**. No Treyce Mail.Send. `invent=false`.

| Invoice | KIMCO | Result | Receipts | Amount | PO |
| --- | --- | --- | --- | --- | --- |
| 11002 | **10007** | **Success** | 24104 100@3 | 300.00 | 59172 |
| 10999 | **10008** | **Success** | 23978 12@10 | 120.00 | 59160 |
| 11003 | **10009** | HOLD price-does-not-match | 24103 2@0.777 | PDF 10.00 / posted 1.55 | 59083 |
| 11004 | **10010** | **Success** | 24106–24111 incl. swapped 24109/24110 | 2600.00 | 59165 |
| 11005 | **10011** | **Success** | 24105 5@30 | 150.00 | 59118 |
| 10998 | **10012** | **Success** | 23979–23982 qty 6@10 | 240.00 | 59158 |
| 10991 | **10013** | HOLD price-does-not-match | 23967 199@0.75 | PDF 199.00 / posted 149.25 | 59148 |
| 10984 | **10014** | **Success** | 23897 1@5 | 5.00 | 59098 |
| 10969 | **10015** | **Success** | 23677 3@45 | 135.00 | 59079 |
| 10968 | **10016** | **Success** | 23682 1@10 | 10.00 | 59080 |
| 10967 | **10017** | **Success** | 23681 10@50 | 500.00 | 59075 |
| 10964 | **10018** | **Success** | 23683 2@75 | 150.00 | 59076 |
| 10962 | **10019** | **Success** | 23520 4@25 | 100.00 | 59042 |
| 10958 | **10020** | **Success** | 23519 4@65 | 260.00 | 59051 |
| 10956 | **10021** | HOLD qty-does-not-match | partial 23516 2@200 | PDF 700.00 / posted 400.00 | 59016 |
| 10955 | **10022** | **Success** | 23518 5@65 | 325.00 | 59037 |
| 10954 | **10023** | **Success** | 23515 8@15 | 120.00 | 59024 |
| 10953 | **10024** | **Success** | 23514 3@65 | 195.00 | 59032 |
| 10952 | **10025** | **Success** | 23481–23486 (3@445, 3@5, 3@5, 9@10, 3@10, 3@25) | 1560.00 | 59025 |
| 10950 | **10026** | **Success** | 23457 2@50; 23895 1@50 | 150.00 | 59023 |
| 10946 | **10027** | **Success** | 23889–23894 (5@25, 5@10, 15@10, 5@5, 5@5, 5@445) | 2600.00 | 58988 |
| 10945 | **10028** | **Success** | 23379 4@65 | 260.00 | 59003 |
| 10939 | **10029** | **Success** | 23280/23281/23282 qty 1@15 | 45.00 | 58998 |
| 10938 | **10031** | **Success** | 23377 16@5; 23378 14@10 | 220.00 | 58991 |
| 10934 | **10030** | **Success** | 23232 15@10 | 150.00 | 58986 |

**New five: 5 Success / 0 HOLD.** Live GET of 10027–10031 matches each PDF
qty/unit/total. 10946 first pass HOLD used rolled qty 5; record GET matched
all six PO58988 leftovers. 10939 first pass HOLD wanted one qty-3 receipt;
three leftover qty-1 @ $15 on PO58998 were selected (NOTE-23). 10938 first
pass false already-entered vs **JMOR Machinery 4779** (vendor 98, $21,025,
2025-08-05 Type 4) — unique invoice # is not a dup across companies; 4779
left untouched; header **10031** on 711. Outlook **Entered in AI**. Never
AI Skipped. Do not void 10007–10031 or 4779. 10009 / 10013 / 10021 left
for Shawn / buyer.

### Another plus-5 AQPC on batch 711 (30-row sheet, 2026-09-16)

Kyle: run another 5 AQPC only; add to batch **711**; update the sheet to
**30 rows** (prior 25 + 5 new). Do not recreate 10007–10031
(11002 / 10999 / 11003 HOLD / 11004 / 11005 / 10998 / 10991 HOLD / 10984 /
10969 / 10968 / 10967 / 10964 / 10962 / 10958 / 10956 HOLD / 10955 / 10954 /
10953 / 10952 / 10950 / 10946 / 10945 / 10939 / 10938 / 10934). Keep HOLD
rows 11003/10009, 10991/10013, 10956/10021 as-is. Skip Kyle-entered
10917/10918/10920/10921. Guest Intuit View-details click (not
`sale/viewed`). No Intuit login. No Treyce Mail.Send. `invent=false`.
Sheet: `runs/AP-run-2026-09-15-aqpc-batch711-30.xlsx`
(also `runs/AP-run-2026-09-15-aqpc-batch711.xlsx`). Same columns as
batch711-25.

Chosen next unflagged payment-requests (Graph still has no 11006+;
10951 / 10949 / 10948 / 10947 not in mailbox): **10933, 10932, 10929,
10928, 10927**. Guest Playwright View-details (no Intuit login /
no storage-state). Reused batch **711**. No Treyce Mail.Send. `invent=false`.

| Invoice | KIMCO | Result | Receipts | Amount | PO |
| --- | --- | --- | --- | --- | --- |
| 11002 | **10007** | **Success** | 24104 100@3 | 300.00 | 59172 |
| 10999 | **10008** | **Success** | 23978 12@10 | 120.00 | 59160 |
| 11003 | **10009** | HOLD price-does-not-match | 24103 2@0.777 | PDF 10.00 / posted 1.55 | 59083 |
| 11004 | **10010** | **Success** | 24106–24111 incl. swapped 24109/24110 | 2600.00 | 59165 |
| 11005 | **10011** | **Success** | 24105 5@30 | 150.00 | 59118 |
| 10998 | **10012** | **Success** | 23979–23982 qty 6@10 | 240.00 | 59158 |
| 10991 | **10013** | HOLD price-does-not-match | 23967 199@0.75 | PDF 199.00 / posted 149.25 | 59148 |
| 10984 | **10014** | **Success** | 23897 1@5 | 5.00 | 59098 |
| 10969 | **10015** | **Success** | 23677 3@45 | 135.00 | 59079 |
| 10968 | **10016** | **Success** | 23682 1@10 | 10.00 | 59080 |
| 10967 | **10017** | **Success** | 23681 10@50 | 500.00 | 59075 |
| 10964 | **10018** | **Success** | 23683 2@75 | 150.00 | 59076 |
| 10962 | **10019** | **Success** | 23520 4@25 | 100.00 | 59042 |
| 10958 | **10020** | **Success** | 23519 4@65 | 260.00 | 59051 |
| 10956 | **10021** | HOLD qty-does-not-match | partial 23516 2@200 | PDF 700.00 / posted 400.00 | 59016 |
| 10955 | **10022** | **Success** | 23518 5@65 | 325.00 | 59037 |
| 10954 | **10023** | **Success** | 23515 8@15 | 120.00 | 59024 |
| 10953 | **10024** | **Success** | 23514 3@65 | 195.00 | 59032 |
| 10952 | **10025** | **Success** | 23481–23486 (3@445, 3@5, 3@5, 9@10, 3@10, 3@25) | 1560.00 | 59025 |
| 10950 | **10026** | **Success** | 23457 2@50; 23895 1@50 | 150.00 | 59023 |
| 10946 | **10027** | **Success** | 23889–23894 (5@25, 5@10, 15@10, 5@5, 5@5, 5@445) | 2600.00 | 58988 |
| 10945 | **10028** | **Success** | 23379 4@65 | 260.00 | 59003 |
| 10939 | **10029** | **Success** | 23280/23281/23282 qty 1@15 | 45.00 | 58998 |
| 10938 | **10031** | **Success** | 23377 16@5; 23378 14@10 | 220.00 | 58991 |
| 10934 | **10030** | **Success** | 23232 15@10 | 150.00 | 58986 |
| 10933 | **10032** | **Success** | 23236 10@20; 23237 1@30; 23238 4@20; 23239 1@5 | 315.00 | 58962 |
| 10932 | **10033** | **Success** | 23235 1@5 | 5.00 | 58982 |
| 10929 | **10034** | **Success** | 23234 4@30 | 120.00 | 58956 |
| 10928 | **10035** | **Success** | 23233 2@275 | 550.00 | 58969 |
| 10927 | **10036** | **Success** | 23242 1@275 | 275.00 | 58963 |

**New five: 5 Success / 0 HOLD. All 30: 27 Success / 3 HOLD.** Live GET of
10032–10036 matches each PDF qty/unit/total. 10933 first pass HOLD used
rolled qty 10; record GET matched four PO58962 leftovers (10@20, 1@30,
4@20, 1@5). Unique invoice # is still not a dup across companies (AQPC
10938 ≠ JMOR 4779). Outlook **Entered in AI**. Never AI Skipped. Do not
void 10007–10036 or 4779. 10009 / 10013 / 10021 left for Shawn / buyer.

### Another plus-5 AQPC on batch 711 (35-row sheet, 2026-09-16)

Kyle: run another 5 AQPC only; add to batch **711**; update the sheet to
**35 rows** (prior 30 + 5 new). Do not recreate 10007–10036
(through 10933/10032, 10932/10033, 10929/10034, 10928/10035, 10927/10036).
Keep HOLD rows 11003/10009, 10991/10013, 10956/10021 as-is. Skip
Kyle-entered 10917/10918/10920/10921. Guest Intuit View-details click
(not `sale/viewed`). No Intuit login. No Treyce Mail.Send. `invent=false`.
Sheet: `runs/AP-run-2026-09-15-aqpc-batch711-35.xlsx`
(also `runs/AP-run-2026-09-15-aqpc-batch711.xlsx`). Same columns as
batch711-30.

Chosen next unflagged payment-requests not already on KIMCO (Graph still
has no 11006+; 10951 / 10949–10947 / 10931 / 10930 / 10923 / 10922 /
10919 not in mailbox; 10921/20/18/17 Kyle; 10916–10697 already-flagged
or already on KIMCO): **10926, 10925, 10924, 10696, 10523**. Guest
Playwright View-details (no Intuit login / no storage-state). Reused
batch **711**. No Treyce Mail.Send. `invent=false`.

| Invoice | KIMCO | Result | Receipts | Amount | PO |
| --- | --- | --- | --- | --- | --- |
| 10926 | **10037** | **Success** | 23241 6@275 | 1650.00 | 58957 |
| 10925 | **10038** | HOLD no-receipts | none (PO 58939 lines 16/15/30/20 exist, zero receipts) | 730.00 | 58939 |
| 10924 | **10039** | **Success** | 23240 15@10 | 150.00 | 58945 |
| 10696 | **10040** | **Success** | 20668 2@20; 20669 1@5; 20670 2@10; 20671 1@8 | 73.00 | 58377 |
| 10523 | **10041** | **Success** | 18652 1@50 | 50.00 | 57794 |

**New five: 4 Success / 1 HOLD. All 35: 31 Success / 4 HOLD.** Live GET of
10037 / 10039 / 10040 / 10041 matches each PDF qty/unit/total. 10696
first pass HOLD used rolled qty 2; record GET matched four PO58377
leftovers. 10523 first pass price HOLD; leftover 18652 1@50 matches the
PDF. 10925 header **10038** + PDF attached; PO 58939 has matching open
lines but nothing received — buyer must receive. Outlook **Entered in AI**
on Success; **Entered with issues** on 10925. Never AI Skipped. Do not
void 10007–10041 or 4779. 10009 / 10013 / 10021 / 10038 left for Shawn /
buyer.

### Another plus-5 AQPC on batch 711 (40-row sheet, 2026-09-16)

Kyle: run another 5 AQPC only; add to batch **711**; update the sheet to
**40 rows** (prior 35 + 5 new). Do not recreate 10007–10041
(through 10926/10037, 10925/10038 HOLD, 10924/10039, 10696/10040,
10523/10041). Keep HOLD rows 11003/10009, 10991/10013, 10925/10038 as-is
(10956/10021 later finished — NOTE-27). Skip Kyle-entered
10917/10918/10920/10921. Guest
Intuit View-details click (not `sale/viewed`). No Intuit login. No
Treyce Mail.Send. `invent=false`. Sheet:
`runs/AP-run-2026-09-15-aqpc-batch711-40.xlsx`
(also `runs/AP-run-2026-09-15-aqpc-batch711.xlsx`). Same columns as
batch711-35.

Chosen next unflagged payment-requests not already on KIMCO (continue
older after 10523; 10522–10382 already on KIMCO or missing): **10381,
9502, 9498, 9352, 9343**. Guest Playwright View-details (no Intuit
login / no storage-state). Reused batch **711**. No Treyce Mail.Send.
`invent=false`.

| Invoice | KIMCO | Result | Receipts | Amount | PO |
| --- | --- | --- | --- | --- | --- |
| 10381 | **10042** | **Success** | 17800 1@50 | 50.00 | 57572 |
| 9502 | **10043** | **Success** | 9567 1@275 | 275.00 | 55311 |
| 9498 | **10044** | **Success** | 9558 20@10 | 200.00 | 55292 |
| 9352 | **10045** | HOLD not-kannon-po | none (PDF billed to BLM FENCE CO.; Paid in Full) | 130.00 | (blank) |
| 9343 | **10046** | **Success** | 8235 20@8 | 160.00 | 54871 |

**New five: 4 Success / 1 HOLD. All 40 at enter: 35 Success / 5 HOLD;
after 10956 finish: 36 Success / 4 HOLD.** Live GET of
10042 / 10043 / 10044 / 10046 matches each PDF qty/unit/total. 9352 first
pass false Type 4 Success — PDF is billed to **BLM FENCE CO. / Leo
Mendez**, no Kannon PO, Paid in Full $130; header **10045** left as HOLD
Entered with issues (do not void). Outlook **Entered in AI** on Success;
**Entered with issues** on 9352. Never AI Skipped. Do not void
10007–10046 or 4779. After finishing 10956/10021 (NOTE-27), remaining
HOLD: 10009 / 10013 / 10038 / 10045 for Shawn / buyer.

### Finish 10956 / 10021 (same-cost leftover, 2026-09-16)

Kyle: close 10956. Vendor applied the same $300 as invoice qty 6 @$50 vs
receipt **23517** qty 2 @$150. Select the leftover; do not PPV; do not
alter receipt unit price; do not leave HOLD. Live GET after Select
Receipts: **23516** 2@200 + **23517** 2@150, Invoice_Amount **700.00**
matches PDF. Outlook upgraded to **Entered in AI**. NOTE-27. Sheet
`runs/AP-run-2026-09-15-aqpc-batch711-40.xlsx` now **36 Success / 4 HOLD**.

### Void too-old AQPC 10040–10046 (Kyle 2026-09-16)

Kyle: reverse the 7 older AQPC headers and **stop entering older AQPC**.
Deselect receipts (`APInvoiceLine` state **Removed** — Konfigure does not
accept Deleted) then leave batch 711. Live DELETE returns 405 (list does
not allow archive); Void=true does not stick on unposted Status=1. Headers
remain GET-able at 10040–10046 with $0 / no receipts / no batch / VOID
comments. Outlook process categories cleared. NOTE-28. Sheet marks those
rows **Voided**. Remaining Aug–Sep rows stay on 711 (33 unposted),
including **11002 / 10007** (untouched).

### Finish 10925 / 10038 (buyer received PO 58939, 2026-09-16)

Kyle / Ruben Perez ~6:24am CT: **"Okay is bee received"** — parts received
on PO 58939. Live leftovers **24112** 16@$5, **24113** 15@$10,
**24114** 30@$10, **24115** 20@$10 match the PDF 4 lines / $730. Select
Receipts posted; Invoice_Amount **730.00**. Outlook upgraded to
**Entered in AI**. Do not leave HOLD when receipts now exist. Sheet
`runs/AP-run-2026-09-15-aqpc-batch711-40.xlsx` now **31 Success / 2 HOLD
/ 7 Voided**. Remaining HOLD: 11003/10009, 10991/10013.

### NOTE-29 over-PPV do not lock receipts (Kyle 2026-09-16)

Selecting an over-PPV leftover locks it so Shawn cannot unreceive, fix
the PO price, and re-receive. **Do not Select Receipts** for a line
outside the PPV gate (≤10% of invoice total **and** bill PPV ≤$100).
Whole bill over-gate → select **zero**. Still create header + attach PDF.
HOLD `price-does-not-match` + `@Shawn McKibben`. Outlook Entered with
issues. Never Success. Never invent receipts.

Live release: deselect **10009 / 24103** (~84% / $8.45 on AQPC 11003 /
PO 59083) and **10013 / 23967** (~25% / $49.75 on AQPC 10991 / PO 59148).
Keep header + PDF. Sheet Why: receipts **NOT** selected per Kyle lock
rule. Shawn can unreceive / fix PO / re-receive.

Konfigure will return HTTP 200 for `APInvoiceLine` `Removed` and then
roll back if `Invoice_Verification_Amount` still equals the selected
receipt total. Persist by setting verification to **0** on the same PUT
(and Remove any over-gate PPV additional charge). Then restore the PDF
verification. 10013 stayed on batch **711**. 10009 was moved to Treyce
batch **712** (`9/15/26 - tw`) with an over-gate PPV $8.45 posted;
both the receipt line and that PPV were removed so 24103 unlocked.
Do not post PPV over the gate.

Live GET after release: **10009** Invoice_Amount **0**, verification
**10.00**, 0 receipt lines, PDF attached, receipt 24103 Invoiced=false /
Selected=false / Locked_PO=false. **10013** Invoice_Amount **0**,
verification **199.00**, 0 receipt lines, PDF attached, receipt 23967
Invoiced=false / Selected=false / Locked_PO=false. Sheet still
**31 Success / 2 HOLD / 7 Voided**.

"""QUALITY V1.2 registry: Treyce 2026-09-10 notes + Treyce-load self-check.

No network I/O. Tests import this so note ids stay one source of truth.
"""

from __future__ import annotations

import re
from typing import Any

from ap_clerk.gates import (
    GATE_AUTO_PAY,
    GATE_BILL_VS_NOISE,
    GATE_PDF_LINK,
    GATE_PO,
    GATE_PREFLIGHT,
    GATE_PRICE,
    GATE_QTY,
    GATE_RECEIPT,
    GATE_ALREADY_ENTERED,
    GATE_VENDOR,
    RESULT_HOLD,
    RESULT_INCOMPLETE,
    RESULT_SKIPPED,
    RESULT_SUCCESS,
)

# Success means Treyce would not need to rework the bill. These notes are the
# 8/16 dry-10 misses that must never report Success again.
TREYCE_NOTES_V12: tuple[dict[str, Any], ...] = (
    {
        "id": "NOTE-01",
        "slug": "insight-msc-pdf-invoice-number",
        "gate": GATE_PREFLIGHT,
        "cases": ("Insight 1809", "MSC/Rob Brown 5157357 vs filename 191471"),
        "8_16_bug": "HOLD parse-error / filename invoice # because PDF was not read (no-pdf-on-vm).",
        "expected": "Invoice # (and PO) from vendor PDF text. Empty PDF → HOLD no-pdf-on-vm. Never Success.",
        "never_success": True,
    },
    {
        "id": "NOTE-02",
        "slug": "technitool-suffix-fees-not-ppv",
        "gate": "fees-vs-ppv",
        "cases": ("Techni-Tool S1387370.001", "$46.20 Fees not PPV"),
        "8_16_bug": "Header used bare S1387370; $46.20 posted as PPV. Treyce had to fix both.",
        "expected": "Suffix from PDF. Supply/fee → Fees and surcharges, never PPV. Fake-Success forbidden.",
        "never_success": True,
    },
    {
        "id": "NOTE-03",
        "slug": "capital-qty-discrepancy",
        "gate": GATE_QTY,
        "cases": ("Capital 26764 qty 2 vs 2.5", "Capital $25 Fees"),
        "8_16_bug": "Qty mismatch claimed Success. Treyce had to HOLD for the buyer.",
        "expected": "HOLD qty-does-not-match. Post $25 as Fees. Never Success.",
        "never_success": True,
    },
    {
        "id": "NOTE-04",
        "slug": "purvis-po-never-type-4",
        "gate": GATE_PO,
        "cases": ("Purvis 32625214 / PO 58926",),
        "8_16_bug": "Receipt invoice entered Invoice_Type 4 with blank PO.",
        "expected": "Parse PO from PDF; Type 3; Select Receipts. Never Success as blank Type 4.",
        "never_success": True,
    },
    {
        "id": "NOTE-05",
        "slug": "oneal-description-line-match",
        "gate": GATE_RECEIPT,
        "cases": ("O'Neal PIPE A500 B BARE 1 SCH 40 → P-1.00 SCH 40-A500 line 3", "~$0.03 rounding"),
        "8_16_bug": "First qty match picked the wrong PO line. Treyce reworked the bill.",
        "expected": "Match description/part. Rounding may be signed PPV. Wrong line is never Success.",
        "never_success": True,
    },
    {
        "id": "NOTE-06",
        "slug": "emj-price-hold-header-and-category",
        "gate": GATE_PRICE,
        "cases": ("EMJ S813859432 large price gap",),
        "8_16_bug": "Correct HOLD but no header/PDF; Outlook AI HOLD. Treyce still had to enter the header.",
        "expected": "HOLD + create header + attach PDF + Outlook Entered with issues. Never Success.",
        "never_success": True,
    },
    {
        "id": "NOTE-07",
        "slug": "toyota-autopay-hold",
        "gate": GATE_AUTO_PAY,
        "cases": ("Toyota Commercial Finance 3320056",),
        "8_16_bug": "Tried as a live PO bill (55483 not on live) instead of auto-pay HOLD.",
        "expected": "HOLD auto-pay. Do not enter in ERP. Never Success.",
        "never_success": True,
    },
    {
        "id": "NOTE-08",
        "slug": "melody-channell-not-noise",
        "gate": GATE_BILL_VS_NOISE,
        "cases": ("Melody Channell vendor invoices",),
        "8_16_bug": "Classified as junk not-a-bill; 4 invoices skipped.",
        "expected": "Known AP sender + invoice PDF is a bill, not Skipped noise. Never Success-as-skip.",
        "never_success": True,
    },
    {
        "id": "NOTE-09",
        "slug": "aqpc-pdf-behind-link",
        "gate": GATE_PDF_LINK,
        "cases": ("AQPC https download link, no attachment",),
        "8_16_bug": "Silent not-a-bill when the PDF was only a link.",
        "expected": (
            "Unauth GET first, then guest browser click-through (Playwright, no "
            "Intuit login / no storage-state required). PDF → header + attach. "
            "True failure after guest View/Download invoice → HOLD pdf-behind-link "
            "(Why names vendor / # / host and that guest browser was tried). "
            "Never Skipped. Never Success."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-10",
        "slug": "gas-supply-misc-vs-check-stop",
        "gate": GATE_BILL_VS_NOISE,
        "cases": ("Gas 0040367887 5 Misc invoices", "true CHECK STOP notice"),
        "8_16_bug": "Blanket CHECK STOP / noise. Misc invoices were not entered.",
        "expected": "Invoice pages → Misc Type 4 item Shop Supplies - G&S. Notice → Skipped/HOLD. Ambiguous amounts → HOLD. Never Success.",
        "never_success": True,
        "deferred": "Shared-total-only Gas packs (no per-invoice Amount Due) still HOLD gas_misc_ambiguous.",
    },
    {
        "id": "NOTE-11",
        "slug": "nova-258145-from-person-not-vendor",
        "gate": GATE_PREFLIGHT,
        "cases": ("Nova Alloys 258145 / From Erica Barrett",),
        "8_18_bug": (
            "Sheet Vendor=Erica Barrett; HOLD preflight-parse because invoice # was tagged "
            "subject; Attach status no-pdf-on-vm even though 2026-08-18_Invoice00258145.PDF "
            "was on disk."
        ),
        "expected": (
            "Vendor=Nova Alloys from PDF/subject, never the From person. PDF is truth: "
            "same # on subject is a hint, not a parse HOLD. Create header + attach. "
            "Why describes THIS bill (no MSC/McQueary slogan). Only HOLD no-pdf if "
            "the file is truly missing or OCR failed. Never Success-as-Erica-Barrett-HOLD."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-12",
        "slug": "msc-70762501-not-rmp",
        "gate": GATE_VENDOR,
        "cases": ("MSC Industrial Supply 70762501 / KIMCO 9967 posted as RMP",),
        "8_18_bug": (
            "8/18 dry-10: parsed MSC Industrial Supply invoice 70762501, sheet Result "
            "Success, KIMCO id 9967. Live GET: vendor=1320-RMP INDUSTRIAL SUPPLY type 4. "
            "names_match treated {industrial, supply} overlap as a match; seed/lookup "
            "posted RMP lookup-id 1320 instead of MSC alias 128."
        ),
        "expected": (
            "MSC does not names_match RMP. Prefer alias MSC→128 over fuzzy sample seeding. "
            "GET after create: posted vendor must strictly match parsed (or known alias). "
            "Else HOLD/Entered with issues vendor-mismatch (parsed X, posted Y). Never Success. "
            "Do not void."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-13",
        "slug": "crosslink-27943-filename-pdf-on-disk",
        "gate": GATE_PREFLIGHT,
        "cases": (
            "Crosslink 27943 / PO 58888",
            "Crosslink 27944 / PO 58909",
            "Crosslink 27946 / PO 58741",
        ),
        "8_18_bug": (
            "Same false preflight-parse HOLD as Nova 258145: invoice # tagged filename "
            "(invoice-27943.pdf) while date/amount/po came from PDF; Attach no-pdf-on-vm "
            "though the PDF was on disk. Fees Packaging/Shop Supplies; Recovery."
        ),
        "expected": (
            "PDF is truth: on disk + same # on subject/filename is not a parse HOLD. "
            "Create header, attach PDF, continue finish gates. Why describes THIS "
            "Crosslink bill (no MSC/McQueary slogan). no-pdf-on-vm forbidden when "
            "pdf_path exists. Same for 27944/58909 and 27946/58741. "
            "Never Success-as-false-parse-HOLD."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-14",
        "slug": "fastenal-txft4100079-qty-and-fees",
        "gate": GATE_RECEIPT,
        "cases": ("Fastenal TXFT4100079 / PO 58692 / KIMCO 9968",),
        "9_11_bug": (
            "8/18-class Success: empty invoice lines → first open receipt on PO "
            "(qty 36) while invoice qty was 35; Why said 'not first qty'. "
            "Shipping & Handling 63.98 was Excel-only; Additional Charge Fees "
            "were never posted. KIMCO 9968."
        ),
        "expected": (
            "Verify receipt qty and merchandise cost against the invoice. "
            "Qty 35 vs 36 → pick 35. Never first-open / second-open-on-po "
            "Success when open receipts differ. Post Additional Charge "
            "Fees and surcharges / F-Fees & Surcharges (63.98) before Success; "
            "sheet Fees column is not enough. Else Incomplete / Entered with issues."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-15",
        "slug": "emj-z250725432-two-lines",
        "gate": GATE_RECEIPT,
        "cases": ("EMJ Z250725432 / PO 58913 / KIMCO 9969",),
        "9_11_bug": (
            "Invoice had two merchandise lines; PDF line parse left lines:[]; "
            "runner Select Receipts’d one open PO receipt and claimed Success. "
            "Sheet did not name the skipped line. Small random-length price gap "
            "was not posted as PPV."
        ),
        "expected": (
            "Parse all merchandise lines from the PDF. Select Receipts for each "
            "matching line; do not stop after one. Unmatched lines → not Success "
            "and Why names the skipped line(s). Random-length unit gap that "
            "passes ≤10% / ≤$100 is PPV, not Fees. Prepaid/shipping-date with "
            "null amount is not a fee."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-16",
        "slug": "gas-multi-invoice-pdf-after-tax",
        "gate": GATE_PREFLIGHT,
        "cases": ("Gas billing01_A3050_c.pdf / 0040370068 pack / KIMCO 9970",),
        "9_11_bug": (
            "One Gas PDF held 6 invoices; runner collapsed to invoice 0040370068 "
            "plus multi-PO Incomplete 9970. Amount 322 was merchandise/before tax, "
            "not Amount Due after tax."
        ),
        "expected": (
            "Split to 6 bill rows (one invoice # each). Per-section amount is "
            "after-tax Amount Due, never Subtotal. Email still counts as 1 touch. "
            "Why notes multi-invoice-pdf page X–Y of N. Do not Success/Incomplete "
            "a single collapsed invoice when N>1 numbers are present."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-17",
        "slug": "insight-1809-already-entered",
        "gate": GATE_ALREADY_ENTERED,
        "cases": ("Insight Controller Services 1809 already on live",),
        "9_11_bug": (
            "Sheet HOLD preflight-parse with MSC/McQueary slogan and no-pdf-on-vm. "
            "1809 was already entered; the false parse gate fired first."
        ),
        "expected": (
            "Before parse-HOLD / create: look up vendor + invoice #. Already "
            "present → HOLD already-entered / duplicate. Why names vendor, "
            "invoice #, existing KIMCO id(s). No McQueary slogan. No no-pdf-on-vm "
            "when the PDF is on disk."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-18",
        "slug": "outlook-ai-skipped-noise",
        "gate": GATE_BILL_VS_NOISE,
        "cases": ("Mailbox noise / Skipped → Outlook AI Skipped 2",),
        "9_11_bug": (
            "Noise Skipped rows were sheet-only with no Outlook category. "
            "Skip-already-flagged did not treat AI Skipped as already-touched."
        ),
        "expected": (
            "Noise gets Outlook category exactly AI Skipped 2 (never AI HOLD). "
            "Sheet Result stays Skipped. Graph missing category → Why "
            "outlook-category-missing: AI Skipped 2. Already-flagged includes "
            "AI Skipped 2 and leftover AI Skipped (no reprocess, no email-cap consume)."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-19",
        "slug": "3p-rachel-bailey-multi-po",
        "gate": GATE_BILL_VS_NOISE,
        "cases": ("3P / Rachel Bailey INV # 142041 / PO # 58766, 58767, 58844",),
        "9_11_bug": (
            "8/18 dry-10 Skipped not-a-bill for Rachel Bailey INV#+PO# subjects. "
            "3P is a real receipt-type multi-PO vendor."
        ),
        "expected": (
            "Never classify 3P / Rachel Bailey INV#+PO# (142041–142044) as not-a-bill. "
            "Receipt-type Invoice_Type 3, not Misc Type 4. "
            "Multi-PO: header Purchase Order blank; Select Receipts per PO by "
            "invoice line part + PO + qty (CPL # is a secondary slip hint only). "
            "sheet lists every PO and selected receipt ids. Unmatched PO/line → "
            "not Success and Why names it. Partial Select Receipts when any line "
            "matches. Outlook is Entered in AI / AI HOLD / "
            "Entered with issues — never AI Skipped 2."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-20",
        "slug": "eastern-metal-818600-not-noise",
        "gate": GATE_BILL_VS_NOISE,
        "cases": ("Eastern Metal Supply 818600 / 818601",),
        "9_11_bug": (
            "8/18 dry-10 Skipped not-a-bill for Invoice : 818600 / 818601 from "
            "EASTERN METAL SUPPLY of TEXAS, INC."
        ),
        "expected": (
            "Invoice + Eastern Metal / EASTERN METAL SUPPLY is a bill (alias 64). "
            "Invoice/INV subject or a PDF invoice attached is never not-a-bill. "
            "Enter or HOLD with real Why. Never AI Skipped 2."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-21",
        "slug": "aqpc-10917-link-download",
        "gate": GATE_PDF_LINK,
        "cases": ("AQPC payment request invoice 10917 / 10918",),
        "9_11_bug": (
            "8/18 dry-10 Skipped not-a-bill + no-pdf-on-vm for American Quality "
            "Powder Coating payment-request emails (invoice behind a link)."
        ),
        "expected": (
            "AQPC / American Quality Powder Coating is an invoice (never Skipped). "
            "Extract the https payment-request / Intuit link, unauth GET (follow "
            "redirects), then guest browser if the GET hits an auth/bot wall or "
            "intermediate HTML. Click View/Download invoice with no Intuit login. "
            "Success → header + attach + PDF-is-truth. True failure after guest "
            "browser → HOLD pdf-behind-link naming vendor, invoice #, host, and "
            "that guest browser was tried. Never tell Kyle to set "
            "AP_CLERK_INTUIT_STORAGE_STATE for AQPC. Never no-pdf-on-vm after a "
            "fetched file. Never AI Skipped."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-22",
        "slug": "kimco-vendor-invoice-never-skip",
        "gate": GATE_BILL_VS_NOISE,
        "cases": ("Any KIMCO-listed vendor that sends an invoice",),
        "9_11_bug": (
            "Listed KIMCO vendors with Invoice/INV subjects were Skipped as "
            "not-a-bill (Eastern Metal, 3P, AQPC)."
        ),
        "expected": (
            "If the supplier is listed in KIMCO (From or subject) and provides "
            "an invoice (PDF, link-PDF, or Invoice/INV subject), never Skipped / "
            "AI Skipped 2 / not-a-bill. Enter or HOLD with a real Why. AI Skipped 2 "
            "is only for true non-vendor noise (statements, payments, PODs)."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-23",
        "slug": "3p-select-receipts-part-po-never-fail-close",
        "gate": GATE_RECEIPT,
        "cases": (
            "3P 142041–142044 / KIMCO 9988–9991 live 9/14 batch 708",
        ),
        "9_14_bug": (
            "Headers created, PDFs attached, invoice lines parsed "
            "(1007044-1 SUBFRAME WELDMENT, 1020592-1 LOWER PLATFORM, …) "
            "but Select Receipts posted zero receipts. Why was blanket "
            "HOLD no receipts after second pass. Open receipts existed on "
            "the listed PO lines. Matcher used invoice-total qty/cost as "
            "the per-line gate and skipped the open-on-PO fallback because "
            "the bill had multiple merchandise lines. CPL # in the subject "
            "was never required."
        ),
        "expected": (
            "Match each invoice line to open receipts on that line's PO by "
            "part / qty / PO. Check every line; select every match. Price "
            "gaps do not skip receipts: qualifying unit-price variance "
            "posts Additional Charge PPV (≤10% of invoice total and ≤$100); "
            "do not invent a $0.02 PPV when amounts add cleanly. Over "
            "threshold → HOLD price-does-not-match + @Shawn McKibben; do "
            "not Select Receipts on the over-PPV line (NOTE-29 / 11003 / "
            "10991). Still select other in-gate lines. 142043: receipt qty 6 / invoice "
            "4 → select qty 4 if the API allows. Fees ≠ PPV. Never "
            "fail-close the whole bill to no-receipts HOLD when some lines "
            "match. Success only if every line is selected and no human "
            "price/qty fix remains."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-24",
        "slug": "leeco-account-statement-skip",
        "gate": GATE_BILL_VS_NOISE,
        "cases": (
            "Leeco Steel Account Statement 2026-08-18 / leftover KIMCO 9985",
            "Julie Hencke 2026-08-18 Past Due Invoices",
        ),
        "9_14_bug": (
            "Leeco Account Statement (PDF lists invoices 617228 / 617448 / "
            "619920 / 619921) was entered as a bill. Filename 1058256.pdf "
            "became the invoice #. KIMCO 9985 was created on API Agent - "
            "9/14/26 (708) before the statement gate. "
            "The word Invoices on a past-due list must not reclassify it as a bill."
        ),
        "expected": (
            "Account Statements / statements-of-account / past-due invoice "
            "lists are not invoices. Subject or PDF body → Skipped "
            "(bill-vs-noise): statement. No header, no Select Receipts, "
            "no Success. Outlook AI Skipped 2. Julie Hencke Past Due Invoices "
            "is the same skip. Do not void leftover 9985."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-25",
        "slug": "legacy-packing-slip-and-line-receipts",
        "gate": GATE_BILL_VS_NOISE,
        "cases": (
            "Legacy Wire Receipt_114745 signed packing slip (not invoice 114745)",
            "Legacy Wire PS-INV103979 / KIMCO 9995 / PO 58807 / $1271.75",
            "Legacy Wire PS-INV103980 / KIMCO 9996 / PO 58802 / $2664.72",
            "Legacy receipt-scan rows 103979 / 120911 / 121051",
        ),
        "9_15_bug": (
            "Sheet row Legacy 114745 HOLD parse-error from filename "
            "2026-08-19_Receipt_2026-08-19_114745.pdf — a signed packing slip, "
            "not an invoice. Same-class receipt scans invented 103979 / 120911 / "
            "121051. PS-INV103979 HOLD no open receipt qty 77 (inch dimension "
            "from 77\" TUBE, not invoice qty). PS-INV103980 HOLD multiple open "
            "receipts on the PO; merchandise cost does not uniquely align / "
            "will not guess first-open — even though every invoice line matched "
            "an open receipt and only freight remained. Prior matcher over-held "
            "on rolled qty / cost uniqueness instead of line matches. Freight "
            "was never Additional Charge Fees. Select Receipts left "
            "held-unfinished."
        ),
        "expected": (
            "Packing slip / POD / signed delivery receipt (Receipt_ filename, "
            "body “packing slip”, signature pages) is non-invoice: disregard. "
            "No HOLD parse-error row, no AI HOLD as a bill, no invented # from "
            "filename. Invoice # exactly as printed on that invoice PDF "
            "(PS-INV103979, never strip to 103979). Match each merchandise "
            "line by part + qty + PO; select those receipts even if other open "
            "receipts exist on the PO. Do not HOLD ambiguous / cost-uniquely-"
            "align when line matches are clear. Freight / shipping / delivery "
            "→ Additional Charge Fees and surcharges; never blocks Select "
            "Receipts. Still no first-open guess when lines do not match. "
            "Do not blank unfinished Select Receipts when lines matched. "
            "Do not rewrite live 9995 / 9996. Never Success if Treyce would "
            "still fix the bill."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (9995, 9996),
    },
    {
        "id": "NOTE-26",
        "slug": "greentree-invoice-from-not-statement",
        "gate": GATE_BILL_VS_NOISE,
        "cases": (
            "Greentree Packaging & Lumber 2026-09-15 sheet: Invoice from …",
        ),
        "9_15_bug": (
            "Weekday 2026-09-15 live-10 (batch 711) wrongly Skipped "
            "`Invoice from Greentree Packaging & Lumber` as "
            "`Skipped (bill-vs-noise): statement`. Outlook AI Skipped 2, "
            "Attach no-pdf-on-vm, empty invoice #. Run log: "
            "`Skipping statement mail: Invoice from Greentree Packaging & "
            "Lumber`. The bill-vs-noise classifier fired on preview/body "
            "`account statement` tokens before inspecting the attached "
            "invoice PDF (PDF-is-truth violated). The email consumed the "
            "10-cap without entering the bill."
        ),
        "expected": (
            "Subject Invoice/INV/bill hint (`Invoice from …`) is never "
            "statement / AI Skipped 2. A real invoice PDF attachment "
            "(Legacy packing-slip classifier) is never statement. If "
            "unsure, download/inspect the PDF first; prefer enter "
            "(header+attach) or bill HOLD over Skip when an invoice PDF "
            "exists. True Account Statements / past-due lists (Leeco, "
            "Julie Hencke) still skip. Do not invent Success. Do not void "
            "unrelated rows."
        ),
        "never_success": True,
        "do_not_void": True,
    },
    {
        "id": "NOTE-27",
        "slug": "aqpc-10956-same-cost-inverted-qty-unit",
        "gate": GATE_RECEIPT,
        "cases": ("AQPC 10956 / KIMCO 10021 / PO 59016 / receipt 23517",),
        "9_16_bug": (
            "Plus-5 batch 711 left 10956 HOLD qty-does-not-match: line 2 "
            "invoice qty 6 @$50 = $300 vs leftover 23517 / PO59016-02 qty 2 "
            "@$150 = $300. Same cost, qty/unit inverted. Partial 23516 only "
            "(posted $400 vs PDF $700)."
        ),
        "expected": (
            "When leftover receipt extended cost uniquely matches the invoice "
            "line total, Select Receipts even if qty and unit are inverted "
            "(10956 / 23517). Same class as 11004 qty+unit swap. Do not PPV. "
            "Do not alter receipt unit price. Do not HOLD qty-does-not-match "
            "when the dollars already match. Never invent receipts."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (10021,),
    },
    {
        "id": "NOTE-28",
        "slug": "aqpc-too-old-before-2026-08-01",
        "gate": "too-old",
        "cases": (
            "AQPC 10696 / 10040",
            "10523 / 10041",
            "10381 / 10042",
            "9502 / 10043",
            "9498 / 10044",
            "9352 / 10045",
            "9343 / 10046",
        ),
        "9_16_bug": (
            "Plus-5 discovery walked older AQPC payment-requests (Jun 2026 "
            "through Apr 2025) into batch 711 as headers 10040–10046."
        ),
        "expected": (
            "AQPC invoice date before 2026-08-01 → skip / do not create a "
            "header. After Aug/Sep AQPC is exhausted, stop — do not walk "
            "older payment-requests into KIMCO. Kyle 2026-09-16: void/reverse "
            "10040–10046; do not re-enter. Never Success. Never invent receipts."
        ),
        "never_success": True,
        "do_not_void": False,
        "leftover_kimco_ids": (),
        "voided_kimco_ids": (10040, 10041, 10042, 10043, 10044, 10045, 10046),
    },
    {
        "id": "NOTE-29",
        "slug": "over-ppv-do-not-select-receipts",
        "gate": GATE_PRICE,
        "cases": (
            "AQPC 11003 / KIMCO 10009 / PO 59083 / receipt 24103",
            "AQPC 10991 / KIMCO 10013 / PO 59148 / receipt 23967",
        ),
        "9_16_bug": (
            "11003 selected leftover 24103 (2 @ $0.777 vs invoice 2 @ $5, "
            "~84% / $8.45). 10991 selected 23967 (199 @ $0.75 vs invoice "
            "199 @ $1, ~25% / $49.75). Selecting locked the receipt so "
            "Shawn could not unreceive, fix the PO price, and re-receive."
        ),
        "expected": (
            "If a leftover is outside the PPV gate (≤10% of invoice total "
            "AND bill PPV ≤$100), do not Select Receipts for that line. "
            "If the whole bill is over-gate, select zero receipts. Still "
            "create header + attach PDF. HOLD price-does-not-match + "
            "@Shawn McKibben. Outlook Entered with issues. Never Success. "
            "Never invent receipts. Kyle 2026-09-16."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (10009, 10013),
    },
    {
        "id": "NOTE-30",
        "slug": "crosslink-reminder-already-entered",
        "gate": GATE_ALREADY_ENTERED,
        "cases": (
            "Crosslink 27447 / 9382",
            "Crosslink 27448 / 9384",
            "Crosslink 27591 / 9587",
        ),
        "9_16_bug": (
            "AP-run-2026-09-16 reminder emails for already-entered Crosslink "
            "27447 / 27448 / 27591. HOLD already-entered is correct."
        ),
        "expected": (
            "Already-entered reminder emails stay HOLD already-entered. "
            "Do not create another header. Do not invent Success. Leave Crosslink alone."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (9382, 9384, 9587),
    },
    {
        "id": "NOTE-31",
        "slug": "priority1-freight-external-no-receipts",
        "gate": "freight-external",
        "cases": ("Priority 1 18030910 / KIMCO 10047",),
        "9_16_bug": (
            "Incomplete 10047: tried Additional Charge Fees and surcharges / "
            "F-Fees & Surcharges for Freight Charge $235.77 (blocked-400). "
            "Priority 1 is a freight company."
        ),
        "expected": (
            "Enter without Select Receipts lines. All charges → Additional Charge "
            "Freight External on InvoiceAdditionalCharges lookup id 1 "
            "(not Fees & Surcharges id 11). Never Success if posted as Fees. "
            "Finish Incomplete 10047 this way if still open."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (10047,),
    },
    {
        "id": "NOTE-32",
        "slug": "mcnichols-vendor-from-po-partial",
        "gate": GATE_VENDOR,
        "cases": ("McNichols 2559543 / PO 58935",),
        "9_16_bug": (
            "Fail vendor missing for billings@e.mcnichols.com though PO 58935 "
            "exists as PO58935-MCNICHOLS CO. Treyce: vendor = 1116-MCNICHOLS."
        ),
        "expected": (
            "If email domain/name fails but a PO exists and the PO vendor name "
            "is a partial match, create the invoice with that PO vendor. "
            "Never invent a vendor id from thin air — only from the PO vendor link."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-33",
        "slug": "emj-no-po-on-pdf-transfer-ap",
        "gate": GATE_PO,
        "cases": ("EMJ Z250741432 / KIMCO 10048",),
        "9_16_bug": (
            "PO missing from the invoice PDF (printed customer PO is RFQ 081026.3). "
            "Daily marked Success then reversed to a fake receipt HOLD. "
            "Treyce commented @Misty McCoy and transferred to Transfer AP."
        ),
        "expected": (
            "No-PO-on-PDF → comment @Shawn McKibben (purchasing) and Transfer AP "
            "batch (destination only). Misty McCoy is not a hard default. "
            "Not a fake receipt HOLD. Do not invent a PO. Never Success."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (10048,),
    },
    {
        "id": "NOTE-34",
        "slug": "metal-supermarkets-inches-qty",
        "gate": GATE_QTY,
        "cases": ("Metal Supermarkets 1091102 / KIMCO 10049 / PO 58919",),
        "9_16_bug": (
            "HOLD qty-does-not-match: invoice qty 262.74 (the dollar amount) vs "
            "PO/receipt qty 32. Invoice is 1 @ 32 inches; PO has 32 inches."
        ),
        "expected": (
            "Do not HOLD qty-does-not-match when the PDF unit is inches/length "
            "and the PO qty is the inch measure (same class as Legacy Wire "
            "rolled-qty miss). Do not use the invoice dollar amount as qty. "
            "Finish 10049 if leftovers match."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (10049,),
    },
    {
        "id": "NOTE-35",
        "slug": "oneal-per-line-ppv-not-rolled",
        "gate": GATE_PRICE,
        "cases": ("O'Neal 14748440 / KIMCO 10050 / PO 58964",),
        "9_16_bug": (
            "HOLD price-does-not-match $714.60 (bogus rolled variance). "
            "Line 1 matches 20 @ 248.4845. Line 2 qty 6 correct; unit "
            "901.97 vs 901.9 → PPV +0.42 only."
        ),
        "expected": (
            "PPV is the per-line unit/amount gap, not a rolled invoice-total "
            "minus PO-total. Post PPV +0.42 on InvoiceAdditionalCharges lookup "
            "id 13. Do not HOLD $714.60. Over-PPV lock (NOTE-29) still applies "
            "for true over-gate gaps. Finish 10050 with line 1 receipt + PPV "
            "$0.42 if still open."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (10050,),
    },
    {
        "id": "NOTE-36",
        "slug": "gas-labeled-total-amount-due",
        "gate": GATE_PREFLIGHT,
        "cases": (
            "Gas 0040374117 / 0011062620 / 0040372952 / 0011054481",
            "Gas 0040374112 / 0011062611",
        ),
        "9_16_bug": (
            "Preflight-parse HOLD gas_misc_ambiguous with empty amounts though "
            "PDFs were on disk. One note: 0040372952 is only 1 invoice."
        ),
        "expected": (
            "Extract after-tax totals from labeled Total / Amount Due "
            "(including label-then-amount on the next line). Never invent "
            "totals. A single-invoice Gas PDF is not 'multiple Misc invoices'. "
            "HOLD only when a labeled total is truly missing."
        ),
        "never_success": True,
    },
    {
        "id": "NOTE-37",
        "slug": "jpsteel-125315-combine-same-item-receipts",
        "gate": GATE_RECEIPT,
        "cases": (
            "JPSteel 125315 / KIMCO 10107 / PO 59128 / receipts 24126+24127",
        ),
        "9_17_bug": (
            "First-pass HOLD Select Receipts blocked-400 on same-PO-line split "
            "24126 8@$33 + 24127 13@$33 though PDF is one line 21@$33=$693. "
            "Kyle 2026-09-17: combining same-item same-unit-cost leftovers is "
            "acceptable. He finished 10107 live; do not re-Select / edit."
        ),
        "expected": (
            "Combine receipt lines of the same item and same unit cost to match "
            "one invoice line (21@$33 = 8@$33 + 13@$33). Select both leftovers "
            "when the unique qty/cost sum matches. Do not HOLD as Select "
            "Receipts blocked-400 when that sum matches. Never invent receipts. "
            "Kyle already finished 10107 — GET-only; do not mutate."
        ),
        "never_success": True,
        "do_not_void": True,
        "do_not_mutate": True,
        "leftover_kimco_ids": (),
        "kyle_finished_kimco_ids": (10107,),
    },
    {
        "id": "NOTE-38",
        "slug": "jpsteel-125316-rounding-ppv-not-hold",
        "gate": GATE_PRICE,
        "cases": (
            "JPSteel 125316 / KIMCO 10108 / PO 59154 posted $1,580.83 vs PDF $1,580.73",
            "JPSteel 125051 / KIMCO 10111 posted $1,130.34 vs PDF $1,130.40",
        ),
        "9_17_bug": (
            "First-pass HOLD after live GET of 10108: PDF/verification $1,580.73 "
            "vs posted $1,580.83 ($0.10 unit-rounding) though receipts 24128/"
            "24129 already matched. Same class 10111: posted $1,130.34 vs PDF "
            "$1,130.40 ($0.06). Kyle 2026-09-17: those gaps are exactly what "
            "signed PPV is for — never HOLD as rounding."
        ),
        "expected": (
            "When Select Receipts already match and posted Invoice_Amount ≠ PDF "
            "by in-gate unit-rounding, must post signed Additional Charge "
            "Purchase Price Variance (lookup id 13) so Invoice_Amount hits the "
            "PDF (125316 / 10108: −$0.10; 125051 / 10111: +$0.06) and report "
            "Success. Never HOLD as rounding. Two-cent gaps stay a match (no "
            "invented PPV). Over-PPV lock (NOTE-29) still applies — do not "
            "Select Receipts on over-gate lines. Never invent receipts. "
            "Do not mutate 10107."
        ),
        "never_success": True,
        "do_not_void": True,
        "leftover_kimco_ids": (),
    },
    {
        "id": "NOTE-39",
        "slug": "exception-category-owner-at-hold",
        "gate": "exception-category",
        "cases": (
            "price HOLD → price_variance / Shawn McKibben",
            "missing / bad PO / PO not on live → missing_po / Shawn McKibben",
            "missing receipts HOLD → missing_receipt / Ruben Perez",
            "already-entered HOLD → already_entered / none / review",
            "pdf-behind-link HOLD → pdf_capture / AP",
            "qty HOLD → quantity_variance / buyer",
        ),
        "9_17_bug": (
            "HOLD / Incomplete Why strings were unstructured slogans. "
            "Treyce could not sort the sheet by cause; exception rate by "
            "category was unmeasurable (Kyle product bar #4 and #6)."
        ),
        "expected": (
            "Every HOLD / Incomplete / Entered-with-issues row gets a stable "
            "Exception category slug and Exception owner at creation "
            "(Stampli-style categorize-at-creation). Why embeds "
            "`category=…; owner=…` plus vendor / invoice # / PO / next action. "
            "Excel columns Exception category and Exception owner. Empty for "
            "Success and true Skipped noise. Optional workbook summary counts "
            "by category only — never invent Success/touchless rates. "
            "Map existing gates only; do not invent new HOLD reasons. "
            "Shawn McKibben oversees Purchasing: price_variance, missing_po "
            "(PO not on live, missing/bad PO). Transfer AP is a destination "
            "batch only — not the Exception owner. Misty McCoy is not a hard "
            "default. Kyle bar: exception Why with owner; measure exception "
            "rate by cause."
        ),
        "never_success": True,
    },
)

TREYCE_FINISH_CHECKLIST: tuple[dict[str, str], ...] = (
    {
        "id": "invoice-number-from-pdf",
        "check": "Invoice # from vendor PDF text, including Techni-Tool-style suffixes (S1387370.001).",
    },
    {
        "id": "po-not-blank-type-4",
        "check": "PO present on PDF → header PO set and Invoice_Type 3, never blank Type 4.",
    },
    {
        "id": "receipt-match-by-description",
        "check": "Receipt/PO line matched by part or description, not the first leftover qty.",
    },
    {
        "id": "qty-matches",
        "check": "Invoice qty equals matched PO/receipt qty; else HOLD for the buyer.",
    },
    {
        "id": "fees-not-ppv",
        "check": "Supply/fee/surcharge amounts are Additional Charge Fees and surcharges, never PPV.",
    },
    {
        "id": "fees-posted-on-bill",
        "check": (
            "Parsed fee amounts are posted as Additional Charge Fees and surcharges "
            "(F-Fees & Surcharges) on the bill before Success. Sheet column is not enough "
            "(Fastenal TXFT4100079)."
        ),
    },
    {
        "id": "ppv-within-rule",
        "check": "PPV only for unit-price gaps vs PO, and only if ≤10% of invoice total AND ≤$100.",
    },
    {
        "id": "pdf-attached",
        "check": "Vendor PDF is attached on the KIMCO header.",
    },
    {
        "id": "select-receipts-when-po",
        "check": "Select Receipts posted when the PO / Select Receipts path applies.",
    },
    {
        "id": "all-invoice-lines-selected",
        "check": (
            "Every merchandise invoice line has a Select Receipts match. "
            "Unmatched lines are named on Why and never silent Success (EMJ Z250725432)."
        ),
    },
    {
        "id": "posted-vendor-matches-parsed",
        "check": (
            "GET after header create: posted KIMCO vendor name/id matches the parsed "
            "vendor (or a known alias for that same vendor). Else HOLD vendor-mismatch, "
            "never Success. Do not void."
        ),
    },
    {
        "id": "all-pos-selected",
        "check": (
            "Multi-PO bills Select Receipts per PO. Unmatched POs are named on Why "
            "and never silent Success (3P 142041)."
        ),
    },
    {
        "id": "partial-select-receipts-never-fail-close",
        "check": (
            "Select Receipts for every matchable invoice line. Never skip the "
            "whole bill after one unmatched line. Never HOLD no-receipts when "
            "some lines have matching PO receipts (3P 9988–9991). Partial "
            "select → Entered with issues, not a zero-receipt HOLD."
        ),
    },
    {
        "id": "combine-same-item-same-unit-receipts",
        "check": (
            "Combine same-item same-unit-cost receipt leftovers to match one "
            "invoice line (JPSteel 125315: 24126 8@$33 + 24127 13@$33 = "
            "21@$33=$693). Do not HOLD Select Receipts blocked-400 when that "
            "unique sum matches. Kyle 2026-09-17."
        ),
    },
)

# Monday 2026-09-14 2:00am America/Chicago live 10 — basics that must not
# regress as false Skip or false Success. Tests stay unit-only until that job.
MONDAY_LIVE10_BASICS: tuple[dict[str, str], ...] = (
    {"id": "kimco-vendor-never-skip", "note": "NOTE-22", "test": "test_never_repeat_kimco_vendor_invoice_never_skip"},
    {"id": "pdf-is-truth-accurate-why", "note": "NOTE-11", "test": "test_never_repeat_nova_258145"},
    {"id": "qty-cost-receipt-and-fees", "note": "NOTE-14", "test": "test_never_repeat_fastenal_txft4100079"},
    {"id": "all-lines-or-named-skip", "note": "NOTE-15", "test": "test_never_repeat_emj_z250725432_two_lines"},
    {"id": "gas-split-after-tax", "note": "NOTE-16", "test": "test_never_repeat_gas_multi_invoice_pdf"},
    {"id": "3p-multi-po-select-receipts", "note": "NOTE-19", "test": "test_never_repeat_3p_select_receipts_cpl"},
    {"id": "aqpc-link-download", "note": "NOTE-21", "test": "test_never_repeat_aqpc_10917_link_download"},
    {"id": "eastern-metal-not-noise", "note": "NOTE-20", "test": "test_never_repeat_eastern_metal_818600_not_noise"},
    {"id": "already-entered-why", "note": "NOTE-17", "test": "test_never_repeat_insight_1809_already_entered"},
    {"id": "ai-skipped-true-noise-only", "note": "NOTE-18", "test": "test_never_repeat_ai_skipped_noise"},
)


COL_EXCEPTION_CATEGORY = "Exception category"
COL_EXCEPTION_OWNER = "Exception owner"

# Stable slugs + who acts next. Map existing gates only; do not invent HOLD reasons.
# Shawn McKibben oversees all Purchasing (Kyle 2026-09-18). Transfer AP is a
# destination batch only — never the Exception owner. Misty McCoy is not a
# hard default for missing_po or Transfer AP contact.
EXCEPTION_CATEGORY_OWNERS: dict[str, str] = {
    "price_variance": "Shawn McKibben",
    "missing_receipt": "Ruben Perez",
    "quantity_variance": "buyer",
    "missing_po": "Shawn McKibben",
    "vendor_mismatch": "AP / vendor master",
    "already_entered": "none / review",
    "pdf_capture": "AP",
    "auto_pay": "none",
    "partial_match": "AP / Treyce",
    "other": "AP",
}

PURCHASING_EXCEPTION_CATEGORIES: frozenset[str] = frozenset(
    {"price_variance", "missing_po"}
)

_EXCEPTION_PREFIX_RE = re.compile(
    r"category=(?P<category>[a-z0-9_]+);\s*owner=(?P<owner>[^.]*)",
    re.I,
)


def exception_prefix(category: str, owner: str) -> str:
    return f"category={category}; owner={owner}"


def canonical_exception_owner(category: str, owner: str | None = None) -> str:
    """Shawn owns purchasing/PO issues. Misty McCoy is not a hard default."""
    mapped = EXCEPTION_CATEGORY_OWNERS[category]
    extracted = (owner or "").strip()
    if category in PURCHASING_EXCEPTION_CATEGORIES:
        return mapped
    if "misty" in extracted.lower():
        return mapped
    return extracted or mapped


def classify_exception(*, result: str | None, why: str | None) -> tuple[str, str] | None:
    """Map a sheet row to (category, owner). None = leave columns blank.

    Success and true Skipped noise stay blank. HOLD / Incomplete / Fail /
    Entered-with-issues get a stable slug from existing gate Why text.
    """
    result_s = (result or "").strip()
    why_s = (why or "").strip()
    if result_s == RESULT_SUCCESS:
        return None
    if result_s == RESULT_SKIPPED or result_s in {"Noise"}:
        return None

    already = _EXCEPTION_PREFIX_RE.search(why_s)
    if already:
        slug = already.group("category").strip().lower()
        owner = already.group("owner").strip()
        if slug in EXCEPTION_CATEGORY_OWNERS:
            return slug, canonical_exception_owner(slug, owner)

    why_l = why_s.lower()
    if (
        GATE_ALREADY_ENTERED in why_l
        or "already-entered" in why_l
        or "already entered" in why_l
    ):
        return "already_entered", EXCEPTION_CATEGORY_OWNERS["already_entered"]
    if GATE_AUTO_PAY in why_l or "auto pay" in why_l:
        return "auto_pay", EXCEPTION_CATEGORY_OWNERS["auto_pay"]
    if GATE_PDF_LINK in why_l or GATE_PREFLIGHT in why_l or "parse-error" in why_l:
        return "pdf_capture", EXCEPTION_CATEGORY_OWNERS["pdf_capture"]
    if GATE_VENDOR in why_l or "vendor-mismatch" in why_l:
        return "vendor_mismatch", EXCEPTION_CATEGORY_OWNERS["vendor_mismatch"]
    if (
        "misty mccoy" in why_l
        or "transfer ap" in why_l
        or "no-po-on-pdf" in why_l
        or "no po on pdf" in why_l
        or "po number is missing" in why_l
        or "hold (po)" in why_l
        or "not findable on live" in why_l
    ):
        return "missing_po", EXCEPTION_CATEGORY_OWNERS["missing_po"]
    if GATE_PRICE in why_l or "price does not match" in why_l:
        return "price_variance", EXCEPTION_CATEGORY_OWNERS["price_variance"]
    if GATE_QTY in why_l or "qty does not match" in why_l:
        return "quantity_variance", EXCEPTION_CATEGORY_OWNERS["quantity_variance"]
    if (
        "selected vs unmatched" in why_l
        or ("partial" in why_l and "select receipts" in why_l)
        or ("unmatched invoice line" in why_l and "selected receipts" in why_l)
    ):
        return "partial_match", EXCEPTION_CATEGORY_OWNERS["partial_match"]
    if (
        "no receipts" in why_l
        or "no open receipt" in why_l
        or "parts not received" in why_l
    ):
        return "missing_receipt", EXCEPTION_CATEGORY_OWNERS["missing_receipt"]
    if "no-pdf" in why_l or "no pdf" in why_l:
        return "pdf_capture", EXCEPTION_CATEGORY_OWNERS["pdf_capture"]
    return "other", EXCEPTION_CATEGORY_OWNERS["other"]


def apply_exception_category_owner(row: dict[str, Any]) -> dict[str, Any]:
    """Stamp Exception category/owner and embed `category=…; owner=…` on Why.

    Success / true Skipped noise leave both columns blank and Why unchanged.
    Idempotent if Why already embeds the canonical prefix. Rewrites a stale
    `missing_po` Misty McCoy / Transfer AP prefix to Shawn McKibben.
    """
    result = str(row.get("Result") or "")
    why = str(row.get("Why") or "").strip()
    classified = classify_exception(result=result, why=why)
    if classified is None:
        row[COL_EXCEPTION_CATEGORY] = ""
        row[COL_EXCEPTION_OWNER] = ""
        return row
    category, owner = classified
    row[COL_EXCEPTION_CATEGORY] = category
    row[COL_EXCEPTION_OWNER] = owner
    prefix = exception_prefix(category, owner)
    already = _EXCEPTION_PREFIX_RE.search(why)
    if already:
        old = already.group(0)
        if old != prefix:
            why = why[: already.start()] + prefix + why[already.end() :]
        row["Why"] = why.strip()
        return row
    row["Why"] = f"{prefix}. {why}" if why else prefix
    return row


def exception_category_counts(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Pareto counts by Exception category. Counts only — no rates."""
    tallies: dict[str, int] = {}
    for row in rows:
        category = str(row.get(COL_EXCEPTION_CATEGORY) or "").strip()
        if not category:
            continue
        tallies[category] = tallies.get(category, 0) + 1
    return sorted(tallies.items(), key=lambda item: (-item[1], item[0]))


def note_ids() -> tuple[str, ...]:
    return tuple(str(note["id"]) for note in TREYCE_NOTES_V12)


def note_by_id(note_id: str) -> dict[str, Any]:
    for note in TREYCE_NOTES_V12:
        if note["id"] == note_id:
            return note
    raise KeyError(note_id)


def success_forbidden_outcomes() -> frozenset[str]:
    """Allowed results when a Treyce-note failure mode fires. Success is not among them."""
    return frozenset({RESULT_HOLD, RESULT_INCOMPLETE, RESULT_SKIPPED, "Fail"})


def assert_never_success(result: str | None, *, note_id: str, detail: str = "") -> None:
    """Hard invariant: a Note miss must never be reportable as Success."""
    if (result or "").strip() == RESULT_SUCCESS:
        extra = f" {detail}" if detail else ""
        raise AssertionError(f"{note_id}: failure mode reported Success.{extra}")

"""QUALITY V1.2 registry: Treyce 2026-09-10 notes + Treyce-load self-check.

No network I/O. Tests import this so note ids stay one source of truth.
"""

from __future__ import annotations

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
        "expected": "Best-effort public GET. Auth wall → HOLD pdf-behind-link, not Skipped. Never Success.",
        "never_success": True,
        "deferred": "Authenticated vendor portals are HOLD pdf-behind-link until Kyle adds a download path.",
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
        "cases": ("Mailbox noise / Skipped → Outlook AI Skipped",),
        "9_11_bug": (
            "Noise Skipped rows were sheet-only with no Outlook category. "
            "Skip-already-flagged did not treat AI Skipped as already-touched."
        ),
        "expected": (
            "Noise gets Outlook category exactly AI Skipped (never AI HOLD). "
            "Sheet Result stays Skipped. Graph missing category → Why "
            "outlook-category-missing: AI Skipped. Already-flagged includes "
            "AI Skipped (no reprocess, no email-cap consume)."
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
            "Multi-PO: header Purchase Order blank; Select Receipts per PO; "
            "sheet lists every PO and selected receipt ids. Unmatched PO/line → "
            "not Success and Why names it. Outlook is Entered in AI / AI HOLD / "
            "Entered with issues — never AI Skipped."
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
            "Invoice + Eastern Metal is a bill (alias 64). Invoice hint or PDF "
            "attached is never not-a-bill. Enter or HOLD with real Why."
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
            "AQPC is an invoice. Extract the https payment-request link and "
            "download the PDF. Auth wall → HOLD pdf-behind-link naming vendor, "
            "invoice #, and link host. Never Skipped noise."
        ),
        "never_success": True,
        "deferred": "Authenticated vendor portals stay HOLD pdf-behind-link.",
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
            "If the supplier is listed in KIMCO and provides an invoice "
            "(PDF, link-PDF, or Invoice/INV subject), never Skipped / AI Skipped "
            "/ not-a-bill. Enter or HOLD with a real Why. AI Skipped is only "
            "for true non-vendor noise."
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
)


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

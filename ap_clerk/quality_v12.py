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
        "deferred": "Live 0040367887 page-accurate 5-way amount split needs that PDF; heuristic HOLDs when ambiguous.",
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
            "Vendor=Nova Alloys from subject/PDF, never the From person. Invoice # from PDF "
            "or same # on subject when PDF exists — not a parse HOLD. Only HOLD no-pdf if "
            "the file is truly missing. Never Success-as-Erica-Barrett-HOLD."
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

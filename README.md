# Kannon AP Clerk

Daily-runnable CLI that enters **header-only** AP invoices on the **KIMCO prototype**. It does not call `live.kimcoerp.com`.

This repository is the AP Clerk only. It does not depend on deer-intelligence, Customer_PO_Automation, Quote_Automation, or Capacity_Analysis.

## What it does

1. Authenticates to KIMCO prototype (`POST /api/v2/authenticate` with `{key, password}`, then Bearer token).
2. Finds or creates today's AP invoice batch named exactly `API Agent - M/D/YY` in `America/Chicago` (example: `API Agent - 8/27/26`).
3. Creates header-only AP invoices for real vendor bills. A missing PO is **not** a HOLD: the header is still created with Purchase Order left blank. Select Receipts is used only when a PO exists.
4. Attempts official 7.7 PDF attach when a PDF is present (notify `POST .../{id}/attachments/upload` → upload to `uploadUrl` → complete `POST .../{id}/attachments`). On this AP list, upload notify is 405. This VM has no inbox PDFs, so the run records `Attach status = no-pdf-on-vm`.
5. Writes `runs/AP-run-YYYY-MM-DD.xlsx`.

## Prototype only

Hard-coded prototype services (do not use live GUIDs):

| List | GUID |
| --- | --- |
| AP invoices | `4898fd433bff417daa1689dece54b840` |
| AP batches | `23245dfe2158496cbf949e7091d0542c` |
| Purchase lines | `f5b4b6f631be45f58d10e019060bd761` |
| Receipts | `0a74fb9972974950a5e24e8f4981aaff` |
| Document types | `c2c451ebb51d42fb96e2651490ee1477` |

Auth: `POST https://prototype.kimcoerp.com/api/v2/authenticate`.

If `KIMCO_PROTOTYPE_INSTANCE_URL` is unset, the CLI uses `https://prototype.kimcoerp.com`. Any URL containing `live.kimcoerp.com` is refused.

## Environment names

Credentials are read from the environment only. Values are never invented and never printed. The CLI prints only present/absent for each name, then uses the **first populated pair**:

1. `KIMCO_PROTOTYPE_API_KEY` / `KIMCO_PROTOTYPE_API_PASSWORD` / `KIMCO_PROTOTYPE_INSTANCE_URL`
2. aliases `KIMCO_API_KEY` / `KIMCO_API_PASSWORD`

If keys are missing, the CLI still writes an Excel report with HOLD/Fail rows and stops before any prototype calls.

## Install and run

```bash
python3 -m pip install -r requirements.txt
python3 -m ap_clerk enter \
  --fixture fixtures/testrun-727-803.json \
  --as-of 2026-08-27 \
  --report runs/AP-run-2026-08-27.xlsx
```

`--as-of` overrides the Chicago calendar date used for batch naming and the default report filename.

## Batch naming

- Exact name: `API Agent - M/D/YY` (no leading zeros).
- Never reuse batch `Mark Brown 8/4/26` (id 669) or any other person's batch.
- Never recreate KIMCO invoices 9474–9478 (batch 670 `API Agent - 8/24/26`) or 9481–9499 (batch 671).

## Header create (PO-bill field list is a hypothesis; no-PO shape taken from existing prototype bills)

Working prototype POST shape:

- `AP_Invoice_Batch` `{id}`
- `Purchase_Order` `{id}` only for single-PO bills (looked up from purchase lines). **Omitted** when there is no PO (same as multi-PO bills).
- `Vendor` `{id}` from an existing prototype invoice for that vendor name
- `Invoice_Number`, `Invoice_Date`, `Invoice_Verification_Amount`
- `Invoice_Type` `3` when a PO is present. Existing prototype no-PO vendor bills use `Invoice_Type` `4` (not 3).
- `Invoice_Due_Date`, `Terms_Code`, `Currency` `{id: 3}`, `Remit_To_Address`, `Transaction_Date`
- `Comments` `API TEST prototype only do not pay.`

Vendor / remit / terms are copied from an existing prototype invoice matched by vendor name. PO id comes from purchase lines when a PO exists. The CLI will not invent a PO or PO lines. If the bill names a PO that is not in prototype, that bill is **HOLD**. If the bill has no PO, the header is still created and Purchase Order is left blank.

If the invoice number already exists on prototype, the CLI does not recreate it and records `Fail/already exists`.

## Fees vs PPV

Fees and surcharges (shop supplies, packaging recovery, fuel/energy surcharge, freight, shipping, admin/account/check fees, garment protection, rental, and similar add-ons) are **not** Purchase Price Variance. They go to Additional Charge **Fees and surcharges** / **F-Fees & Surcharges**, and they are recorded in the Excel **Fees and surcharges** column. The CLI does not post a PPV additional charge for those.

PPV is only a merchandise unit-price gap vs the PO, and only if that gap is under 10% of the invoice total. Otherwise HOLD and return to purchasing. The 7/27–8/3 test pack has no known PPV cases.

Terms `1/2% 10 - Net 30` means Net 30 due plus an optional 0.5% discount if paid in 10 days. It is not a different due date.

## Select Receipts gap

Lines must be added in the KIMCO UI via **Select Receipts**, not typed **Add Item**, and **only when a PO exists**. No-PO headers leave Purchase Order blank and do not get invented PO lines. The API cannot Select Receipts until Editable is on. This AP list rejects PUT/edit and attach POST with 405 (`list does not allow items to be edited`). The CLI does **not** invent line POSTs that skip Select Receipts. Successful PO-bill header rows record lines as blocked/405 in the Excel **Why** column.

## HOLD rules

- Real vendor bills with no PO: **create the header**. Do not HOLD just because there is no PO.
- HOLD remains for CHECK STOP, statements, PODs, payment letters, dups, and not-a-bill.
- Gas and Supply `0040325801`: CHECK STOP, HOLD, no header.
- Skip statements, PODs, payment letters, and dups (already filtered from the fixture).
- Do not delete or void anything.

## Excel report

`runs/AP-run-YYYY-MM-DD.xlsx` columns:

Vendor, Invoice #, date, PO, Amount, Result (Success/Fail/HOLD), Why, KIMCO id, Batch, Fees and surcharges, PPV, Attach status.

One row per invoice in `fixtures/testrun-727-803.json`.

## Safety

- Prototype host only. Live is never called.
- Secret values are never printed.
- No invoice is deleted or voided.

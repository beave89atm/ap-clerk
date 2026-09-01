# Kannon AP Clerk

Weekday 5:00am America/Chicago CLI that enters **header-only** AP invoices. Default target is the **KIMCO prototype**. The scheduled 30-invoice run is **live** and requires `--live`.

**Live writes require `--live` (or `KIMCO_TARGET=live`) plus `KIMCO_LIVE_*`.** Kyle said go for the first live 20-invoice test on 2026-08-28. Default target remains prototype. Never use prototype keys against live.

This repository is the AP Clerk only. It does not depend on deer-intelligence, Customer_PO_Automation, Quote_Automation, or Capacity_Analysis.

## What it does

1. Authenticates to KIMCO (`POST /api/v2/authenticate` with `{key, password}`, then Bearer token). Default host is prototype.
2. Finds or creates today's AP invoice batch named exactly `API Agent - M/D/YY` in `America/Chicago` (example: `API Agent - 8/27/26`).
3. Creates header-only AP invoices for real vendor bills. A missing PO is **not** a HOLD: the header is still created with Purchase Order left blank. Select Receipts is used only when a PO exists.
4. Attempts official 7.7 PDF attach when a PDF is present (notify `POST .../{id}/attachments/upload` → upload to `uploadUrl` → complete `POST .../{id}/attachments`). On this AP list, upload notify is 405. This VM has no inbox PDFs, so the run records `Attach status = no-pdf-on-vm`.
5. After enter, writes an Outlook category on `accountspayable@kannonmfg.com` only. **Success** gets the preexisting category `Entered in AI` (capital E). **HOLD / Fail** (CHECK STOP, missing vendor, already-exists, not-a-bill, no receipts the CLI will not guess) get red category **`AI HOLD`**. Never both on the same message. Does **not** use the Outlook follow-up flag or `AP Matched`.
6. Writes `runs/AP-run-YYYY-MM-DD.xlsx`. The weekday daily run emails that workbook to `Treyce at kannonmfg.com` FROM `accountspayable@kannonmfg.com`.

## Prototype (default)

Hard-coded prototype services (do not use live GUIDs on prototype):

| List | GUID |
| --- | --- |
| AP invoices | `4898fd433bff417daa1689dece54b840` |
| AP batches | `23245dfe2158496cbf949e7091d0542c` |
| Purchase lines | `f5b4b6f631be45f58d10e019060bd761` |
| Receipts | `0a74fb9972974950a5e24e8f4981aaff` |
| Document types | `c2c451ebb51d42fb96e2651490ee1477` |

Auth: `POST https://prototype.kimcoerp.com/api/v2/authenticate`.

If `KIMCO_PROTOTYPE_INSTANCE_URL` is unset, the CLI uses `https://prototype.kimcoerp.com`. Prototype target refuses any URL containing `live.kimcoerp.com`.

## Live (Kyle said go)

`--live` or `KIMCO_TARGET=live` selects live. Default is still off. The CLI refuses that target unless `KIMCO_LIVE_API_KEY` and `KIMCO_LIVE_API_PASSWORD` are both present. It never uses prototype keys against live, and never uses live keys against prototype.

If `KIMCO_LIVE_INSTANCE_URL` is unset, the live target uses `https://live.kimcoerp.com`. After Kyle said go, `enter --live` and `daily --live` authenticate and then create today's `API Agent - M/D/YY` batch plus invoice headers on live. Application `Mail.ReadWrite` is granted on the Kannon AP Clerk Entra app (admin consent 2026-08-28). After enter, the CLI PATCHes `Entered in AI` on Success and `AI HOLD` on HOLD/Fail, on `accountspayable@kannonmfg.com` only. It does not set `flag.flagStatus`.

`enter --live --from-inbox --limit 20` pulls the 20 most recent vendor-invoice PDFs from `accountspayable@kannonmfg.com` that are not already `Entered in AI` (skips statements/PODs/CHECK STOP/payment confirmations and replaces them so 20 real bills are still attempted), then processes oldest-first among those. It does not drain the mailbox.

## Weekday 5:00am daily run of 30

Kyle wants this scheduled. Every weekday at **5:00am America/Chicago**, process **30** unprocessed vendor invoices from `accountspayable@kannonmfg.com` only.

**Queue (FIFO toward today, not newest-30, not mailbox-oldest):**

- Start at received/invoice date **2026-07-28** inclusive (`America/Chicago`).
- Work **forward toward the most current**.
- Skip messages already categorized `Entered in AI`.
- Still skip not-a-bill / statement / POD / CHECK STOP / payment and **replace** so 30 real bills are attempted when possible. Those skips get `AI HOLD` and a HOLD Excel row with the same why.
- Persist `runs/daily-cursor.json` (last processed `receivedDateTime` + message id). The next weekday **continues after the previous 30**. It does not restart at 7/28 every morning.

**Enter on LIVE KIMCO (`--live`):** batch `API Agent - M/D/YY` America/Chicago. Same header rules as the 8/27 live test, plus Treyce 8/28 / Kyle PPV: no-PO still gets a header; Select Receipts when a PO exists (part + PO/WO line, slip # = invoice # before HOLD-no-receipts); fees and surcharges vs signed PPV (10% of invoice total **and** ≤ $100, else price-does-not-match HOLD); invoice # as printed; invoice date from the PDF not the email; vendor from the live PO / aliases 1386 and 1410; PDF attach; HOLD also for CHECK STOP / statements / PODs / dups / not-a-bill / price does not match.

**Email:** after the run, send `runs/AP-run-YYYY-MM-DD.xlsx` to `Treyce at kannonmfg.com` FROM `accountspayable@kannonmfg.com` via Graph `sendMail`. Subject `AP run YYYY-MM-DD`. Body is short: Success / Fail / HOLD counts and batch id. If Application `Mail.Send` is missing (403), the xlsx is still written and the run records `email-denied` without crashing the enter path.

**How it is invoked (Grok Bot 5am routine — not a GitHub Actions live cron):**

```bash
python -m ap_clerk daily --live --limit 30
```

`daily` **requires** `--live` and live creds. There is no GitHub Actions schedule that would post live from CI without Kyle. The 5am launch is the documented command above.

Graph permission probe (does **not** send mail, does **not** post KIMCO):

```bash
python3 -m ap_clerk probe
```

Creates (or records 403 for) master category `AI HOLD`, then creates and deletes a draft on `accountspayable@kannonmfg.com` only. It never calls `sendMail`.

**Weekday FIFO 30 (2026-09-01 America/Chicago):** continued after the 8/31 Marmon cursor (`2026-07-30T06:29:03Z`, processed_count 70). Batch `API Agent - 9/1/26` id **692**. 30 bills attempted (34 statement/POD/not-a-bill skips replaced). **19 Success** headers **9711–9729**, **7 Fail**, **4 HOLD** bills (2 Capital Machine no-receipts on PO 58634; Telecom 16960 and Willbanks 209661 price-does-not-match) plus skip HOLD rows. Success got `Entered in AI` + `flag.flagStatus=flagged`. Mail.Send to Treyce: **email-sent**. Cursor: `runs/daily-cursor.json` last_received **2026-08-01T13:46:16Z**. Report: `runs/AP-run-2026-09-01.xlsx`. Attach notify still **405**. Vendor ids from live GET: Capital Machine=45, Willbanks=202, Shoppas=159, Eastern Metal=64, UniFirst First Aid=209, Clear Kut=345, TPI=183.

**Weekday FIFO 30 (2026-08-31 America/Chicago):** batch `API Agent - 8/31/26` id **689**. 30 bills attempted from 2026-07-28 (no prior cursor; skip `Entered in AI`). **14 Success** headers **9678–9691**, **7 Fail** (5 already-exists; 2 Amada work-order reports), **9 HOLD** bills (3 price-does-not-match + 6 no-receipts) plus 40 not-a-bill/statement skip rows. Success also got `flag.flagStatus=flagged`. Mail.Send to Treyce: **email-sent**. Cursor: `runs/daily-cursor.json` last_received **2026-07-30T06:29:03Z**. Report: `runs/AP-run-2026-08-31.xlsx`. Attach notify still **405**.

**First live 20-invoice test (2026-08-27 America/Chicago, Kyle said go):** batch `API Agent - 8/27/26` id **688**. 15 Success headers **9663–9677**, 5 Fail, 0 HOLD. Outlook was not flagged. PDF attach notify returned **405**. Vendor-missing leftovers: National Specialty Alloys `453743` and Coherent Corp. `120953` (PO exists on live; vendor id was not found; not invented). Report: `runs/AP-run-2026-08-27.xlsx`.

**Select Receipts + PDF attach (2026-08-28, live web login):** GUI work on the same 15 Success headers only. No new headers or batches. Outlook not flagged in that GUI pass. 9/10 PO headers had receipts selected; Fastenal `TXFT499356` / 9677 is HOLD-no-receipts (no qty-6 slip on live). All 15 vendor PDFs attached on the header (Graph match by vendor + invoice #). Fees posted as **F-Fees & Surcharges** (McMaster 68.93, Modern Heat 26.25, Fastenal 92.05 / 21.79 / 21.70). PPV posted only on Fastenal `TXFT499646` (4.80). Fail rows were not touched.

**Mail.ReadWrite / `Entered in AI` (2026-08-28):** After admin consent, Graph can write the AP mailbox. Kyle's preexisting category is exactly `Entered in AI` (confirmed on existing messages; `outlook/masterCategories` GET was 403). That category is the process marker. The follow-up flag and `AP Matched` are not. 14 already-Success 8/27 headers (9663–9676) received `Entered in AI`. HOLD/Fail rows were not marked. No KIMCO writes. See Outlook section below.

Identified 2026-08-28 with GET only (auth success; token not printed; zero live records written):

| List | GUID | Confirmed by GET |
| --- | --- | --- |
| AP invoices | `bcca4094b6ec4564942b19f5d7bb255c` | HTTP 200; fields include `Invoice_Number`, `Vendor`, `Purchase_Order`, `Invoice_Type`, `AP_Invoice_Batch` |
| AP invoice batches | `31bf524dcd5b464580d4a1b55c01881e` | HTTP 200; fields include `AP_Invoice_Batch_ID`, `Batch_Owner`, `Description`, `Status` |
| Purchase lines | `f1f8732f8daa4e2b9d8065037f7bb43d` | HTTP 200; fields include `Purchase_Order_Number`, `Purchase_Line_Number`, `Quantity` |
| Receipts | `494eafafa31a42bba7eb8697a36a3f0a` | HTTP 200; fields include `Name`, `PO_Item_Number`, `Quantity_Received`, `Receipt` |

Live AP invoice list: default list columns are sparse (`Invoice_Number`, `Invoice_Balance`, `Vendor_$_Display_Name`, `Purchase_Order`, `Closed`, `Void`). Item GET returns a full header. No `Editable` field. Create-only was **not** proven (no POST/PUT to test). GET attachments on an existing invoice returned 200.

No live batches named `API Agent - M/D/YY` were found (687 batches scanned). Existing names are person-dated (for example `8/25/26 - tw`).

## Environment names

Credentials are read from the environment only. Values are never invented and never printed. The CLI prints only present/absent for each name.

**Prototype** uses the first populated pair:

1. `KIMCO_PROTOTYPE_API_KEY` / `KIMCO_PROTOTYPE_API_PASSWORD` / `KIMCO_PROTOTYPE_INSTANCE_URL`
2. aliases `KIMCO_API_KEY` / `KIMCO_API_PASSWORD`

**Live** (only with `--live` or `KIMCO_TARGET=live`) uses:

- `KIMCO_LIVE_API_KEY` / `KIMCO_LIVE_API_PASSWORD` / `KIMCO_LIVE_INSTANCE_URL`

**Outlook / Graph** (same client-credentials as Mail.Read; mailbox writes need Application `Mail.ReadWrite`; daily email needs Application `Mail.Send`, granted 2026-08-28):

- `MICROSOFT_GRAPH_TENANT_ID` / `MICROSOFT_GRAPH_CLIENT_ID` / `MICROSOFT_GRAPH_CLIENT_SECRET`
- optional `AP_CLERK_REPORT_TO` (defaults to Treyce at kannonmfg.com)

If keys are missing, the CLI still writes an Excel report with HOLD/Fail rows and stops before any KIMCO calls.

## Install and run

```bash
python3 -m pip install -r requirements.txt
python3 -m ap_clerk enter \
  --fixture fixtures/testrun-727-803.json \
  --as-of 2026-08-27 \
  --report runs/AP-run-2026-08-27.xlsx
```

`--as-of` overrides the Chicago calendar date used for batch naming and the default report filename.

`--live` is off by default. First live 20-invoice test (Kyle said go):

```bash
python3 -m ap_clerk enter --live --from-inbox --limit 20
```

Weekday 5:00am America/Chicago (Grok Bot launches this; requires live creds + `--live`):

```bash
python3 -m ap_clerk daily --live --limit 30
```

Graph probe (AP mailbox draft only; does **not** send mail; does **not** post KIMCO):

```bash
python3 -m ap_clerk probe
```

Inbox pull (read-only; does **not** write categories):

```bash
python3 -m ap_clerk pull \
  --inbox-from 2026-07-27 \
  --inbox-to 2026-08-03 \
  --out runs/inbox-unflagged.json
```

`--match-inbox` on `enter` attaches Graph message ids onto fixture invoices, then flags **after** a Success header create — never after download alone. `--mailbox` must be `accountspayable@kannonmfg.com`; any other mailbox is rejected.

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
- `Invoice_Type` `3` when a PO is present. Existing prototype no-PO vendor bills use `Invoice_Type` `4` (GUI label **Miscellaneous**), not 3.
- `Invoice_Due_Date`, `Terms_Code`, `Currency` `{id: 3}`, `Remit_To_Address`, `Transaction_Date`
- `Comments` `API TEST prototype only do not pay.`

Vendor / remit / terms are copied from an existing prototype invoice matched by vendor name. PO id comes from purchase lines when a PO exists. The CLI will not invent a PO or PO lines. If the bill names a PO that is not in prototype, that bill is **HOLD**. If the bill has no PO, the header is still created and Purchase Order is left blank.

If the invoice number already exists on prototype, the CLI does not recreate it and records `Fail/already exists`.

## Fees vs PPV (Kyle, 2026-08-28)

Fees and surcharges (shop supplies, packaging recovery, fuel/energy surcharge, freight, shipping & handling, admin/account/check fees, garment protection, rental, and similar add-ons) are **not** Purchase Price Variance. They go to Additional Charge **Fees and surcharges** / **F-Fees & Surcharges**, and they are recorded in the Excel **Fees and surcharges** column. Never post those as PPV.

When an invoice **line** amount does not match the PO **line** amount:

- Post Additional Charge **Purchase Price Variance** (signed; negative is allowed) **only if both**: `|variance|` ≤ 10% of the **invoice total** **and** total PPV on that bill ≤ **$100**.
- If the variance is **over 10% of invoice total or over $100**, do **not** post PPV. **HOLD / AI HOLD** as **price does not match**. Purchasing must unreceive, change the PO price, and re-receive. Add a comment on the PO line for **@Shawn McKibben**. Do **not** alter receipt unit price in GI (that breaks WO cost, material cost, and PO clearing).
- **$0 PO unit price** is a price-does-not-match HOLD (Modern Heat pattern), not a PPV.

Worked examples:

| Case | Decision |
| --- | --- |
| EMJ 770.16 vs 752.10 on a $752.10 invoice (2.4%, under $100) | PPV **−18.06** |
| O'Neal $0.10 rounding | PPV **−0.10** |
| $120 gap on a $2000 invoice (6% but >$100) | price does not match |
| $50 gap on a $400 invoice (12.5%) | price does not match |

Terms `1/2% 10 - Net 30` means Net 30 due plus an optional 0.5% discount if paid in 10 days. It is not a different due date.

## Treyce 2026-08-28 notes

1. **Vendor lookup:** if name match fails, use the vendor on the live PO. Aliases: National Specialty Alloys = vendor **1386**; Coherent Corp. = vendor **1410**. Do not Fail “vendor missing” when the PO has a vendor.
2. **Invoice number as printed.** Modern Heat Treat: `8-220804` not `220804` (vendor prefix). Do not invent prefixes; learn from the invoice PDF or a known vendor pattern.
3. **Invoice date** = the date printed on the invoice, not the email received date (Telecom 17602 is 8/26 not 8/27).
4. **Select Receipts:** match invoice **part numbers** and PO/WO lines, not the first qty that fits. Modern Heat 220804 was lines **6–7** (parts `625-5200-002` and `400-5200-001`), not lines 1–3.
5. Search receipts harder before HOLD-no-receipts (Fastenal `TXFT499356` was findable). Try slip # = invoice #, then part, qty, PO line.
6. Categories unchanged: Success = `Entered in AI`; HOLD/Fail = `AI HOLD`.

## Select Receipts

Lines must be added in the KIMCO UI via **Select Receipts**, not typed **Add Item**, and **only when a PO exists**. No-PO headers leave Purchase Order blank and do not get invented PO lines. The API cannot Select Receipts until Editable is on. This AP list rejects PUT/edit and attach POST with 405 (`list does not allow items to be edited`). The CLI does **not** invent line POSTs that skip Select Receipts. Successful PO-bill header rows record lines as blocked/405 in the Excel **Why** column.

Matching (Treyce 2026-08-28): pick the receipt whose **part number** and **PO/WO line** match the invoice line. Do not take the first leftover qty that fits. Search order before HOLD-no-receipts: **slip # = invoice #**, part, qty, PO line. Fastenal `TXFT499356` is findable that way. Modern Heat `8-220804` is PO lines 6–7, not 1–3. If receipts were searched and none match, HOLD / `AI HOLD` (no header). If receipts were not loaded, the CLI does not invent a HOLD-no-receipts.

## HOLD rules

- Real vendor bills with no PO: **create the header**. Do not HOLD just because there is no PO.
- HOLD remains for CHECK STOP, statements, PODs, payment letters, dups, and not-a-bill.
- **Price does not match** (Kyle 2026-08-28): HOLD / `AI HOLD` when the merchandise line gap is over 10% of invoice total or over $100, or when the PO unit price is $0. Do not post PPV. Purchasing unreceives, changes the PO price, and re-receives. Comment **@Shawn McKibben**. Do not change GI receipt unit price.
- HOLD-no-receipts only after a thorough search (slip # = invoice #, part, qty, PO line) finds nothing. Fastenal `TXFT499356` was findable and must not HOLD for that reason.
- Gas and Supply `0040325801`: CHECK STOP, HOLD, no header.
- Skip statements, PODs, payment letters, and dups (already filtered from the fixture).
- Do not delete or void anything.

## Excel report

`runs/AP-run-YYYY-MM-DD.xlsx` columns:

Vendor, Invoice #, date, PO, Amount, Result (Success/Fail/HOLD), Why, KIMCO id, Batch, Fees and surcharges, PPV, Attach status, Flag in Outlook, Flag status.

**Flag in Outlook:** `Yes` when a process category is applied (Success, HOLD, or Fail). **Flag status** is `entered-in-ai` / `ai-hold` / skipped reasons (`no-message-id`, `graph-denied`, `skipped-not-success`). The Why column already has the HOLD/Fail details.

One row per invoice in `fixtures/testrun-727-803.json`. Why also notes `Flag status=...` when a category was attempted.

## Outlook categories after match

The only mailbox this CLI will touch is `accountspayable@kannonmfg.com`. Mail without category `Entered in AI` is the work queue; that category means already processed. `AI HOLD` means this run could not process the message.

When an invoice is pulled from that mailbox:

- **Success** (header created): PATCH categories to include `Entered in AI` and **remove** `AI HOLD` if present.
- **Unable to process** (HOLD, Fail, CHECK STOP, missing vendor, already-exists, no receipts the CLI will not guess, not-a-bill): PATCH categories to include exact string **`AI HOLD`** and **remove** `Entered in AI` if present.

PATCH body is `{"categories":[<existing except AP Matched and the other process marker>, "<Entered in AI|AI HOLD>"]}`. It does **not** set `flag.flagStatus`. It does **not** add `AP Matched`. Never apply `AI HOLD` and `Entered in AI` on the same message.

Graph message id is kept on the run so the category is applied after match, not after download/`pull` alone.

`Mail.ReadWrite` (Application) is required for the message PATCH. Same `MICROSOFT_GRAPH_*` client-credentials as Mail.Read. A 403 is recorded as `graph-denied`; the CLI does not invent another mailbox.

**Red `AI HOLD` master category:** the CLI POSTs `/users/accountspayable@kannonmfg.com/outlook/masterCategories` with `displayName` `AI HOLD` and color `preset0` (Red). `GET masterCategories` was **403** on 2026-08-28. **Create POST was also 403** on 2026-08-28 (`MailboxSettings.ReadWrite` not in the token). The CLI still PATCHes the message categories with the exact string `AI HOLD`. Kyle may need to set that category color to red once in Outlook, or grant MailboxSettings.ReadWrite.

**Mail.Send (2026-08-28, after Kyle granted Application Mail.Send):** Token role `Mail.Send` is **present**. `probe` created a draft on `accountspayable@kannonmfg.com` only (HTTP **201**) and deleted it (HTTP **204**). `sendMail` was **not** called. No mail was sent to Treyce or anyone else. Daily will use `sendMail` after a real `--live` enter. Report: `runs/graph-send-probe-2026-08-28.json`.

**Category lookup (2026-08-28):** `GET .../outlook/masterCategories` returned **403** (`ErrorAccessDenied`). Existing AP messages already carry the preexisting category exactly **`Entered in AI`** (3 messages; follow-up flag not set on those). Other categories seen on listed mail: `Solved!`, `Investigating`, `Purchasing Investigating`, `No KC Receipt`, `Problems/Issues`, `Partial Receipt`, `Cost Discrepancy`.

**Mail.ReadWrite probe (2026-08-28, earlier same day):** Graph token OK (Mail.Read). Telecom `Invoice - 16960` (KIMCO 9481) uniquely identified; PATCH follow-up flag returned **403**. Left unchanged.

**Follow-up-flag probe (2026-08-28, after admin consent, superseded):** Telecom `17601` / 9663 was PATCHed `flagStatus=flagged` and `AP Matched`. Kyle then said the marker is `Entered in AI`, not the follow-up flag.

**`Entered in AI` apply (2026-08-28):** 14 already-Success 8/27 messages (9663–9676) uniquely identified (vendor + invoice # + PDF; Air Products / EMJ / O'Neal / Gas & Supply confirmed via PDF text). Category PATCH **200** on all 14; GET after each showed `Entered in AI`. Telecom 17601: removed `AP Matched`, cleared `flagStatus` back to `notFlagged`. Skipped: 9677 Fastenal `TXFT499356` (HOLD-no-receipts) and Fail rows NSA `453743`, Coherent `120953`, Fastenal `TXFT496725` / `TXFT499639` / `TXFT499528`. No KIMCO writes. Report: `runs/entered-in-ai-2026-08-28.json`.

## Safety

- Default target is prototype. Live writes only with `--live` + `KIMCO_LIVE_*` after Kyle said go.
- Secret values are never printed.
- No invoice is deleted or voided.
- Live never uses prototype keys. Prototype never writes to `live.kimcoerp.com`.
- The only Outlook mailbox this CLI will read or mark is `accountspayable@kannonmfg.com`. Apply `Entered in AI` after Success and `AI HOLD` after HOLD/Fail. Never both. Never use the follow-up flag or `AP Matched` as the process marker.
- `daily` requires `--live`. Do not add a GitHub Actions cron that posts live without Kyle.

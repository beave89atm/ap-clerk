# Kannon AP Clerk

Weekday America/Chicago CLI that enters AP invoices. Default target is the **KIMCO prototype**. The scheduled 30-invoice run is **live** and requires `--live`.

**QUALITY V1.1:** `Success` means a **finished bill**, not a header create. Header-only (blocked-405 attach or Select Receipts not posted) is **Incomplete**. This build does **not** run a live 30 and does **not** Mail.Send to Treyce. The weekday routine stays paused until a supervised 10-invoice live dry run after merge. Live 2026-09-09 probe (one existing header) proved record GET/PUT/attach; see below.

**Live writes require `--live` (or `KIMCO_TARGET=live`) plus `KIMCO_LIVE_*`.** Kyle said go for the first live 20-invoice test on 2026-08-28. Default target remains prototype. Never use prototype keys against live.

This repository is the AP Clerk only. It does not depend on deer-intelligence, Customer_PO_Automation, Quote_Automation, or Capacity_Analysis.

## What it does

1. Authenticates to KIMCO (`POST /api/v2/authenticate` with `{key, password}`, then Bearer token). Default host is prototype.
2. Finds or creates today's AP invoice batch named exactly `API Agent - M/D/YY` in `America/Chicago` (example: `API Agent - 8/27/26`).
3. Applies QUALITY V1.1 hard gates (below). Real vendor bills still get a header when gates allow. A missing PO is **not** a HOLD by itself; a PO printed on the invoice or findable on live by vendor + part/WO must be set (never Misc Type 4 in that case). Select Receipts is required when a PO exists.
4. Attempts official 7.7 PDF attach on the **invoice record** when a PDF is present (notify `POST /api/v2/{AP_INVOICE_GUID}/{id}/attachments/upload` → PUT `uploadUrl` with `x-ms-blob-type: BlockBlob` → complete `POST /api/v2/{AP_INVOICE_GUID}/{id}/attachments`). Updates, line additions, and Select Receipts-equivalent edits use the same record URL, never the bare list GUID. Live probe 2026-09-09: record GET/PUT/attach returned 200 after Kyle enabled the four list checkboxes. A later 405 still means check **Can Edit Items / Inline**.
5. After enter, writes an Outlook category on `accountspayable@kannonmfg.com` only. **Success** (finished bill) gets the preexisting category `Entered in AI` (capital E). **Incomplete / HOLD / Fail** get red category **`AI HOLD`**. Never both on the same message. Does **not** set `flag.flagStatus=flagged` and does **not** use `AP Matched`.
6. Writes `runs/AP-run-YYYY-MM-DD.xlsx`. The weekday daily run emails that workbook to `Treyce at kannonmfg.com` FROM `accountspayable@kannonmfg.com` only after a real `--live` enter. This QUALITY V1.1 PR does not send that mail.

## QUALITY V1.1 gates

`Success` is returned only when **all** of the following are clear. Tests in `tests/test_gates.py` fail if `Success` is returned without attach + receipts (when a PO exists).

| Result | Meaning | Outlook |
| --- | --- | --- |
| **Success** | Header created **and** (if PO: Select Receipts done) **and** vendor PDF attached on the header. | `Entered in AI` only |
| **Incomplete** | Header created but attach is blocked-405 / missing **or** receipts not selected. Not a finished bill. | `AI HOLD` (never `entered-in-ai`) |
| **HOLD** | Gate failed before a trustworthy finished bill. Why names the gate. | `AI HOLD` |
| **Fail** | Already exists, vendor missing, or create HTTP error. | `AI HOLD` |

**Why** must name which gate failed. The **Notes** column is left empty for Treyce.

1. **Preflight parse** — invoice #, date, amount, and PO are taken from the **vendor PDF text**, not email subject/filename alone. If the PDF total cannot be verified: `HOLD parse-error (preflight-parse)`, no wrong-amount header. Reject `Purchase_Order_*.pdf` and any attachment that is a PO, not an invoice. Regressions: Gas `0040323616` wrong amount; MSC/McQueary invoice # from filename; Legacy `Purchase_Order_58861.pdf`.
2. **PO** — never leave header Purchase Order blank when a PO is on the invoice **or** findable on live by vendor + part/WO. Multi-PO: header PO blank is OK; Select Receipts per PO is required. If the printed PO is wrong/missing, search live POs by vendor + part numbers before Misc Type 4. Regressions: RMP `1470159`; Willbanks `209663` / `209664` “not misc, has PO”.
3. **Receipt** — before HOLD-no-receipts, second pass: slip # = invoice #, part, qty, PO line, then all open receipts on that PO. Match part/PO-WO lines (Modern Heat), not first qty. Regressions: Capital `26167`; Fastenal `TXFT499356`.
4. **Finish** — `Success` requires header + (Select Receipts when PO) + PDF attached. Header-only with blocked-405 attach or receipts not selected = **Incomplete**, not Success, not Entered in AI. Live 2026-09-09 (after Kyle enabled the four list checkboxes): record GET/PUT/PDF attach work API-only. Select Receipts is a record **PUT** of `lists.APInvoiceLine` (see below). No-PO bills can finish API-only (header + PDF). Do not open the KIMCO UI for attach.
5. **Bill vs noise** — vendor invoices with a PDF must enter (American Quality Powder Coating). Statements, payments, CHECK STOP, internal mail, and PODs stay HOLD. Noise does not count toward the 30-bill attempt quota; skips are replaced so 30 real bills are still attempted.
6. **Teaching loop** — every Treyce note above has a regression test. `tests/` fails if Success is returned without attach + receipts (when PO).

**Also:** fees/surcharges (including freight) are never double-counted as PPV in Excel. PPV only under Kyle’s ≤10% of invoice total **and** ≤$100 rule; else price-does-not-match HOLD + `@Shawn McKibben`. Vendor lookup uses the PO vendor id and known aliases when the printed name fails (NSA 1386, Coherent 1410).

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

## KIMCO AP Invoice list (Kyle / developer guidance)

The AP Invoice list must have these four checkboxes enabled in **KIMCO admin** (Kyle enables; this repo does not open the KIMCO UI):

1. **Can View Items**
2. **Can Edit Items**
3. **Quick Add**
4. **Can Edit Items Inline**

**Record-endpoint rule:**

| Operation | Endpoint |
| --- | --- |
| Create header (POST) | List: `/api/v2/{AP_INVOICE_GUID}` |
| Search / list (GET) | List: `/api/v2/{AP_INVOICE_GUID}` |
| Update existing invoice (PUT/PATCH) | Record: `/api/v2/{AP_INVOICE_GUID}/{invoice_id}` |
| Line additions / Select Receipts-equivalent | Record: `/api/v2/{AP_INVOICE_GUID}/{invoice_id}` |
| PDF attach notify + complete | Record: `/api/v2/{AP_INVOICE_GUID}/{invoice_id}/attachments/upload` then `.../{invoice_id}/attachments` |

Live host is `https://live.kimcoerp.com`; prototype uses the same path shape on `https://prototype.kimcoerp.com` with the prototype GUID. The client refuses PUT/PATCH (and attach POST) on the bare list GUID.

A 405 **after** those record URLs means the list still is not editable: **check Can Edit Items / Inline on the list**.

**Live record-endpoint probe (2026-09-09, after Kyle enabled the four checkboxes; Expose as Web Service stayed on):** Orthman Incomplete header **9931** (`701684`, PO 58636, paused dry-run batch 701). No 10/30, no Mail.Send, no KIMCO UI, no void/delete.

| Call | URL | HTTP |
| --- | --- | --- |
| Auth | `POST /api/v2/authenticate` | **200** |
| GET record | `/api/v2/bcca4094b6ec4564942b19f5d7bb255c/9931` | **200** |
| GET attachments | `.../9931/attachments` | **200** (1 existing vendor PDF) |
| OPTIONS record | same record URL | **405** (`Allow: DELETE, GET, PUT` — OPTIONS itself rejected; PUT is allowed) |
| PUT record | same record URL, same `Comments` | **200** |
| Attach notify | `POST .../9931/attachments/upload` | **200** (`uploadUrl` + `fileId` present; `method=PUT`) |
| Blob upload without `x-ms-blob-type` | notify `uploadUrl` | **400** `MissingRequiredHeader` / `x-ms-blob-type` |
| Blob upload with `x-ms-blob-type: BlockBlob` | notify `uploadUrl` | **201** |
| Attach complete | `POST .../9931/attachments` `{fileId}` | **200** |
| GET attachments after | `.../9931/attachments` | **200** (count 2; same vendor filename) |

Before those list checkboxes: list-GUID attach notify was **405**. After: record GET/PUT/attach are **not** 405. **Finish-gate can go API-only for header update + PDF attach.** Graph PDF came from `accountspayable@` search `Invoice 701684 from Orthman` (saved dry-run message id 404). Secrets / signed URLs not printed.

## Live (Kyle said go)

`--live` or `KIMCO_TARGET=live` selects live. Default is still off. The CLI refuses that target unless `KIMCO_LIVE_API_KEY` and `KIMCO_LIVE_API_PASSWORD` are both present. It never uses prototype keys against live, and never uses live keys against prototype.

If `KIMCO_LIVE_INSTANCE_URL` is unset, the live target uses `https://live.kimcoerp.com`. After Kyle said go, `enter --live` and `daily --live` authenticate and then create today's `API Agent - M/D/YY` batch plus invoice headers on live. Application `Mail.ReadWrite` is granted on the Kannon AP Clerk Entra app (admin consent 2026-08-28). After enter, the CLI PATCHes `Entered in AI` on Success only and `AI HOLD` on Incomplete/HOLD/Fail, on `accountspayable@kannonmfg.com` only. It does not set `flag.flagStatus`. QUALITY V1.1 does not run that live 30.

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

**Weekday FIFO 30 (2026-09-08 America/Chicago):** continued after the 9/7 Air Products cursor (`2026-08-12T01:41:44Z`, processed_count 324). Batch `API Agent - 9/8/26` id **701**. 30 bills attempted (60 statement/payment/not-a-bill skips replaced; O'Neal 15439109 + 15439230 from one batched PDF). **24 Success** headers **9897–9920**, **3 Fail** (already-exists: Clear Kut V009928/V009914, Capital 25641), **3 HOLD** bills (JP Steel 124747, Austin 2490201, EMJ S813605432 price-does-not-match) plus skip HOLD rows. Success got `Entered in AI` + `flag.flagStatus=flagged`. Mail.Send to Treyce: **email-sent**. Cursor: `runs/daily-cursor.json` last_received **2026-08-14T21:19:47Z**. Report: `runs/AP-run-2026-09-08.xlsx`. Attach notify still **405**. Vendor ids from live GET: O'Neal=137, PCT Support=140, Xcaliber=339, Morgan Steel=304. Note: Hapeco 6114659/6114660 headers left Purchase Order blank (PO 58562/58563 exist on live purchase lines but were not indexed without a PO id). Xcaliber freight $48.67 is Fees and surcharges; Excel also recorded it as PPV (API does not post Additional Charge).

**Weekday FIFO 30 (2026-09-07 America/Chicago):** continued after the 9/4 RMP cursor (`2026-08-07T23:01:54Z`, processed_count 274). Batch `API Agent - 9/7/26` id **700**. 30 bills attempted (20 statement/CHECK STOP/not-a-bill skips replaced; Precision 19334–19339 all selected from one multi-PDF email). **9 Success** headers **9888–9896**, **20 Fail** (already-exists on live), **1 HOLD** bill (Fastenal TXFT499875 price-does-not-match) plus skip HOLD rows. Success got `Entered in AI` + `flag.flagStatus=flagged`. Mail.Send to Treyce: **email-sent**. Cursor: `runs/daily-cursor.json` last_received **2026-08-12T01:41:44Z**. Report: `runs/AP-run-2026-09-07.xlsx`. Attach notify still **405**. Vendor ids from live GET: UniFirst Corp=189, Precision Fabrication=144, Versalift=178, Automated Finishing=331, Polymer Products=358, Hapeco=384, McMaster=117, Air Products=13, EMJ=208. Note: **9888** `58861` was created from Legacy `Purchase_Order_58861.pdf` on the PS-INV103969 email (not a vendor invoice). Left as-is (do not void). Later inbox skips `Purchase_Order_*.pdf`.

**Weekday FIFO 30 (2026-09-04 America/Chicago):** continued after the 9/2 O'Neal cursor (`2026-08-05T04:26:33Z`, processed_count 201). Batch `API Agent - 9/4/26` id **697**. 30 bills attempted (43 statement/CHECK STOP/not-a-bill skips replaced). **3 Success** headers **9877–9879**, **24 Fail** (already-exists on live after vendor-alias retry), **3 HOLD** bills (Telecom 17042 / 17142 and Beshert JVT SI-43650 price-does-not-match) plus skip HOLD rows. Success got `Entered in AI`. Mail.Send to Treyce: **email-sent**. Cursor: `runs/daily-cursor.json` last_received **2026-08-07T23:01:54Z**. Report: `runs/AP-run-2026-09-04.xlsx`. Attach notify still **405**. Vendor ids from live GET: Leeco=109, Austin Hardware=34, A1 Image=8, Maynard Nexsen=116, Gexpro=73, Legacy Wire=292, Beshert=37. Note: 9878 `103946` and 9879 `103963` were created from mailbox sender "Giovanny"; printed numbers `PS-INV103946` / `PS-INV103963` already existed as 9800 / 9844 (not voided).

**Weekday FIFO 30 (2026-09-02 America/Chicago):** continued after the 9/1 Waste Connections cursor (`2026-08-01T13:46:16Z`, processed_count 134). Batch `API Agent - 9/2/26` id **694**. 30 bills attempted (37 statement/POD/not-a-bill skips replaced). **18 Success** headers **9757–9774**, **10 Fail** (already-exists on live + ENGIE vendor-missing), **2 HOLD** bills (McMaster 69440053 no receipts on PO 58840; EMJ T605093432 price-does-not-match 23.3%) plus skip HOLD rows. Success got `Entered in AI` + `flag.flagStatus=flagged`. Mail.Send to Treyce: **email-sent**. Cursor: `runs/daily-cursor.json` last_received **2026-08-05T04:26:33Z**. Report: `runs/AP-run-2026-09-02.xlsx`. Attach notify still **405**. Vendor ids from live GET: NTEX=134, GRM=78, Kloeckner=106, Morgan Steel=304, American Bearing=20, Crosslink=278, Ryerson=152, Alternative Parts=215 (not 25 DO-NOT-USE), Tube Supply=341 (not 334 Special Metals), Lavanture=295, McQueary=119, Hudson=88.

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
6. Categories: Success = `Entered in AI`; Incomplete/HOLD/Fail = `AI HOLD`. Never set `flag.flagStatus=flagged`.

## Select Receipts

Lines must come from **Select Receipts** (or the API record-endpoint equivalent), not typed **Add Item**, and **only when a PO exists**. No-PO headers leave Purchase Order blank and do not get invented PO lines.

**Record PUT (reverse-engineered 2026-09-09 from GET of UI-finished bills 9663 / 9670 / 9672):** invoice record GET returns child rows at `lists.APInvoiceLine`. Each line's `values.Receipt.id` is the **receipt LINE** id from `/api/v2/{RECEIPTS_GUID}/{id}` (not the parent `Receipt` header object on that row). OPTIONS on the invoice record is `Allow: DELETE, GET, PUT`. The client PUTs that same child shape — never the list GUID, never typed merchandise Add Item.

```http
PUT /api/v2/bcca4094b6ec4564942b19f5d7bb255c/{invoice_id}
```

```json
{
  "lists": {
    "APInvoiceLine": [
      {"values": {"Receipt": {"id": 23879}}}
    ]
  }
}
```

GET after a successful Select Receipts shows `values.Lines_Count` > 0 and `lists.APInvoiceLine[].values.Receipt.id` set, plus Quantity / Purchase_Order_Line / Part_ID filled by KIMCO (same as the UI). Example from live 9663 (Telecom 17601, already selected in the 2026-08-28 GUI pass): line id 19771, `Receipt.id` **23228** (slip 106620 qty 16, part A-04421-000), `Lines_Count` 1, `Total_Line_Net_Amounts` 1738.56.

If that record PUT still returns 405, check **Can Edit Items / Inline** on the list.

Matching (Treyce 2026-08-28): pick the receipt whose **part number** and **PO/WO line** match the invoice line. Do not take the first leftover qty that fits. Search order before HOLD-no-receipts: **slip # = invoice #**, part, qty, PO line. Fastenal `TXFT499356` is findable that way. Modern Heat `8-220804` is PO lines 6–7, not 1–3. If receipts were searched and none match, HOLD / `AI HOLD` (no header). If receipts were not loaded, the CLI does not invent a HOLD-no-receipts.

Live write target for the first API proof: Orthman Incomplete **9931** (invoice 701684, PO 58636). Open receipts on that PO net to receipt LINE **23879** (PO58636-06, part `1007038-1`, qty 24, slip 58636) — Shawn added PO line 6 for the difference. Slips 701599 on lines 01–05 are already invoiced on vendor invoice 701599. Do not type Add Item.

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

Vendor, Invoice #, date, PO, Amount, Result (Success/Incomplete/HOLD/Fail), Why, KIMCO id, Batch, Fees and surcharges, PPV, Attach status, Flag in Outlook, Flag status, Notes.

**Flag in Outlook:** `Yes` when a process category is applied (Success, Incomplete, HOLD, or Fail). **Flag status** is `entered-in-ai` (Success only) / `ai-hold` (Incomplete/HOLD/Fail) / skipped reasons (`no-message-id`, `graph-denied`, `skipped-not-success`). Incomplete must not say `entered-in-ai`. **Notes** is left empty for Treyce. Why names the gate that failed.

One row per invoice in `fixtures/testrun-727-803.json`. Why also notes `Flag status=...` when a category was attempted.

## Outlook categories after match

The only mailbox this CLI will touch is `accountspayable@kannonmfg.com`. Mail without category `Entered in AI` is the work queue; that category means already processed. `AI HOLD` means this run could not process the message.

When an invoice is pulled from that mailbox:

- **Success** (finished bill only): PATCH categories to include `Entered in AI` and **remove** `AI HOLD` if present.
- **Unable to finish** (Incomplete, HOLD, Fail, CHECK STOP, missing vendor, already-exists, no receipts, not-a-bill, parse-error): PATCH categories to include exact string **`AI HOLD`** and **remove** `Entered in AI` if present.

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
- QUALITY V1.1 does not run a live 30 and does not Mail.Send to Treyce. After merge, run a supervised 10-invoice live dry run before re-arming the weekday 2am routine.

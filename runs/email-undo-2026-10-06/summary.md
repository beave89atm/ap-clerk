# Email undo dry run 2026-10-06 (PHASE 1, read-only)

Rows: 476 (Undo plan 233, Not restored 243)
September messages listed live: 759

## Counts per status

| Status | Count |
|---|---|
| AMBIGUOUS | 1 |
| CATEGORY_ONLY | 227 |
| CONFLICT | 16 |
| OK_TO_MOVE | 232 |

## Planned destination x status

| Destination | Status | Count |
|---|---|---|
| 9 - FORT WORTH ARCHIVE | CATEGORY_ONLY | 66 |
| 9 - FORT WORTH ARCHIVE | CONFLICT | 3 |
| 9 - FORT WORTH ARCHIVE | OK_TO_MOVE | 40 |
| Inbox | AMBIGUOUS | 1 |
| Inbox | CATEGORY_ONLY | 161 |
| Inbox | CONFLICT | 13 |
| Inbox | OK_TO_MOVE | 172 |
| Sent Items | OK_TO_MOVE | 18 |
| UNNAMED (FID 0x0113) | OK_TO_MOVE | 1 |
| UNNAMED (FID 0x376dc5ed3) | OK_TO_MOVE | 1 |

## Pre-run folder ids resolved live

- `...i7GBv6N_AAAAAAEJAAA=` -> {'live_id': 'AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgAuAAAAAAAggjN_BHG4TI2Iz-JKRqfaAQDXcyWbp23lSoY0i7GBv6N_AAAAAAEJAAA=', 'name': 'Sent Items', 'path': 'Sent Items'}
- `...i7GBv6N_AAAAAAEMAAA=` -> {'live_id': 'AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgAuAAAAAAAggjN_BHG4TI2Iz-JKRqfaAQDXcyWbp23lSoY0i7GBv6N_AAAAAAEMAAA=', 'name': 'Inbox', 'path': 'Inbox'}
- `...i7GBv6N_AAAAAAETAAA=` -> {'live_id': 'AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgAuAAAAAAAggjN_BHG4TI2Iz-JKRqfaAQDXcyWbp23lSoY0i7GBv6N_AAAAAAETAAA=', 'name': 'Junk Email', 'path': 'Junk Email'}
- `...i7GBv6N_AACDH-y1AAA=` -> {'live_id': 'AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgAuAAAAAAAggjN_BHG4TI2Iz-JKRqfaAQDXcyWbp23lSoY0i7GBv6N_AACDH-y1AAA=', 'name': '9 - FORT WORTH ARCHIVE', 'path': 'Inbox/9 - FORT WORTH ARCHIVE'}
- `...i7GBv6N_AAN23F7TAAA=` -> {'live_id': 'AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgAuAAAAAAAggjN_BHG4TI2Iz-JKRqfaAQDXcyWbp23lSoY0i7GBv6N_AAN23F7TAAA=', 'name': '3 - RECEIPT INVOICE ISSUES', 'path': 'Inbox/3 - RECEIPT INVOICE ISSUES'}

## Non-OK rows

| Row | Status | Received CT | Sender | Live subject | Current folder | Categories | Destination | Notes |
|---|---|---|---|---|---|---|---|---|
| U006 | CONFLICT | 2026-09-01 08:22 | Amtech Manufacturing, Inc |  | Inbox | AI Needs Review | 9 - FORT WORTH ARCHIVE | unique timestamp match but sender/subject/invoice not corroborated |
| U046 | CONFLICT | 2026-09-04 13:38 | Meridian Finance, Llc | ***IMPORTANT*** 09/09/2026 IPFS Notice of Cancellation for non-payment | Inbox | AI Needs Review | 9 - FORT WORTH ARCHIVE | unique timestamp match but sender/subject/invoice not corroborated |
| U060 | CONFLICT | 2026-09-08 11:15 | Meridian Finance, Llc | RE: ***IMPORTANT*** 09/09/2026 IPFS Notice of Cancellation for non-pay | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U074 | CONFLICT | 2026-09-09 11:49 | Precision Fabrication Services | 19466, 19467, 19468 | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U106 | CONFLICT | 2026-09-12 12:20 | Farley Rd. Amtech Manufacturingkannon Menufacturing, Inc | Invoice 93669897 for Packing List 2154079 | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U184 | CONFLICT | 2026-09-25 06:13 | Kloeckner Metals Corporation | Kannon Manufacturing Inc - Proactive Reminder | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U199 | CONFLICT | 2026-09-28 23:09 | Meridian Finance, Llc | NOTICE OF INTENT TO CANCEL for KANNON MANUFACTURING INC - TXH-F22041 - | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U203 | CONFLICT | 2026-09-29 08:18 | Alternative Parts Inc | Attached is the Invoice for Kannon Mfg dated 9/28/2026. | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U214 | CONFLICT | 2026-09-29 13:49 | Tex Industries, Inc | Invoice # 26-2891 | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U215 | CONFLICT | 2026-09-29 13:50 | Tex Industries, Inc | Invoice # 26-2892 | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U217 | CONFLICT | 2026-09-29 13:51 | Tex Industries, Inc | Invoice # 26-2893 | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U227 | CONFLICT | 2026-09-30 11:35 | Meridian Finance, Llc | IPFS NOTICE OF INTENT TO CANCEL-EFFECTIVE 10-11-2026- Non payment Inst | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| U230 | CONFLICT | 2026-09-30 14:39 | Precision Fabrication Services | 19584, 19585, 19586, 19587, 19588 | Inbox | AI Needs Review | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| N037 | CONFLICT | 2026-09-02 11:40 | Earle M. Jorgensen Co | Kannon Mfg/EMJ skipped invoice | Inbox/9 - FORT WORTH ARCHIVE | Entered in AI | 9 - FORT WORTH ARCHIVE | unique timestamp match but sender/subject/invoice not corroborated |
| N070 | AMBIGUOUS | 2026-09-05 01:21 | McMaster-Carr |  |  |  | Inbox | candidates: Inbox/9 - FORT WORTH ARCHIVE / Credit from Your Order 58889 / AI Skipped 2 // Inbox/9 - FORT WORTH ARCHIVE / Invoice for Your Order 59125 / Entered in AI |
| N108 | CONFLICT | 2026-09-09 16:04 | O'Neal Steel - Dallas (GP) | ORDER PENDING PAYMENT STATUS - PLEASE ADVISE BUYER - 14748440 | Inbox/9 - FORT WORTH ARCHIVE | Entered in AI | Inbox | unique timestamp match but sender/subject/invoice not corroborated |
| N167 | CONFLICT | 2026-09-21 13:49 | Please Pay Upon Receipt Kannon Manufacturing, Inc | MSC Past Due Notification     (DXED#20262641447213976405) | Inbox/9 - FORT WORTH ARCHIVE | AI Skipped 2 | Inbox | unique timestamp match but sender/subject/invoice not corroborated |

No message was moved, re-categorized, flagged, deleted, sent or replied to. KIMCO was not accessed.

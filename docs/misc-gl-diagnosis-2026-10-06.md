# Miscellaneous purchase GL diagnosis (2026-10-06)

Read-only. Signed in through `KimcoClient.authenticate` as API Agent, user 175 (live). First login fetched the named bills. A second login scanned invoice numbers and older samples. No third login. No KIMCO POST, PUT, PATCH, or DELETE other than the two authenticate calls.

## Root cause

KIMCO does not store `Purchase_GL_Account` on a miscellaneous `APInvoiceLine` when the line is added through the API. The add returns HTTP 200, the miscellaneous item, quantity, price, and description save, and the purchase GL comes back null. The child list then refuses edits: a follow-up PUT with `state: "Modified"` returns HTTP 400 and `The list does not allow items to be edited`. A `state: "Removed"` PUT on bill 10407 returned HTTP 200 and left the lines in place.

The GL that shows on older bills is written when a person posts the bill, from the miscellaneous item's own default account. It is not written by the API add. Gas bill 10135 is the proof: the 2026-09-18 script sent `Purchase_GL_Account: {"id": 200}` , the immediate readback in `runs/kimco-gas-misc-lines-10135.json` was `gl: null`, and after Kyle posted it on 2026-09-22 the same lines (20694 and 20695) show `5081100 - Shop Supplies - G&S`.

There is no second field under another name. Blank lines and posted lines use the same property, `Purchase_GL_Account`. There is no `GL_Account` on the line. `Purchase_Discount_GL_Account` is also null until post, then filled from the item (or from header account 5075100 when the item has no discount account of its own).

## What actually has a purchase GL

| Bill | Invoice | What it is | Purchase GL on the line now |
| --- | --- | --- | --- |
| 9732 | Capital 26167 | Posted by Treyce 2026-09-01. Item 46. | **177 / 6455100 Equipment Repair & Maint.** |
| 9827 | Luxor 362341 | Posted by Treyce 2026-09-03. Item 47, split by worker. | **77 / 6510100 Contract Labor** |
| 9911, 10070, 10255 | Luxor 363206, 364200, 365340 | Posted by Treyce. Item 47. | **77 / 6510100** on every line |
| 9902 | PCT 23053 | Posted by Treyce 2026-09-08. Item 51. | **1 / 9999999 Suspense - KIM** (not a dedicated IT account) |
| 5153 | Capital 17306 | Posted 2025-10-03. Item 13 Machine Repairs. | **6 / 2599100 Misc Clearing** |
| 10135, 10128, 10284 | Gas | API add on or after 2026-09-18 left GL null. Kyle posted them. Item 31. | **200 / 5081100** |
| 10394, 10395 | RMP 1473284, 1473396 | API added the lines 2026-09-30 16:28, unposted, no GL in the note. Treyce posted 2026-10-02 11:54. Item 53. | **209 / 5079100**. Discount GL is 153 / 5075100 |
| 10417, 10418 | Shoppa's | API 2026-10-02 said GL did not save. Kyle posted 2026-10-05 16:57. Item 46. | **177 / 6455100** |
| 10419 | UniFirst 2810814758 | Same pattern. Kyle posted 2026-10-05 16:57. | Line 1 item 28 **57 / 6035100**. Line 2 item 31 **200 / 5081100** |
| 10421, 10434 | MSC | API said 5079100 did not save. Kyle posted 2026-10-05 16:57–16:59. Item 53. | **209 / 5079100** |
| 10429, 10430, 10431 | Gas | API sent GL id 200. Readback null. Kyle posted 2026-10-05 16:59. Item 31. | **200 / 5081100** |
| 10356, 10363, 10357 | Air Products, MSC 77062711, UniFirst First Aid | Posted. **No APInvoiceLine rows.** Shop supplies are Additional Charges, lookup id 11 `F-Fees & Surcharges`. | No purchase-GL field on a charge. The charge code carries the account at post. |

## What is still blank

These lines have the miscellaneous item set and `Purchase_GL_Account: null`. All are unposted. Modifier on the line is API Agent.

| Bill | Invoice | Lines | Item | Wanted GL | What a post would book, from the posted samples above |
| --- | --- | --- | --- | --- | --- |
| 10452 | MSC 82328681 | 1 | 53 Machining Tools | 5079100 | 209 / 5079100. Matches. |
| 10453 | RMP 1474130 | 1 | 53 | (item 53) | 209 / 5079100 |
| 10461 | RMP 1474266 | 2 | 53 | (item 53) | 209 / 5079100 |
| 10457 | UniFirst 2810818189 | 2 | 28 then 31 | 6035100 and 5081100 | 57 / 6035100 and 200 / 5081100. Matches. |
| 10458 | Capital PS-INV103813 | 3 | 46 | 6455100 | 177 / 6455100. Matches. |
| 10459 | Capital PS-INV103814 | 3 | 46 | 6455100 | 177 / 6455100. Matches. |
| 10407 | Capital PS-INV103602 | 3 | **13 Machine Repairs** | Kyle asked for equipment repair 6455100 | **6 / 2599100 Misc Clearing**, the account on posted bill 5153. Does not match 6455100. |
| 10433 | Luxor 367008 | 1 | 47 | Comment claims 6510100 | 77 / 6510100. The field is null today. One line, qty 1, price 2990.75, description null. Not split by worker the way 362341 and the other posted Luxor bills are. |
| 10436 | PCT 23115 | 1 | 51 | Comment says confirm at post | Posted twin 23053 booked **9999999 Suspense**, not a normal IT expense account. |
| 10396 | MSC 79166301 | 1 | 53 | Treated as a bill that saved | Null. Unposted since 2026-09-30 16:28. |
| 10397 | MSC 60517341 | 1 | 53 | Treated as a bill that saved | Null. Unposted since 2026-09-30 16:28. |
| 10387 | PCT LS-8578 | 1 | 51 | Used as the pattern for 10436 | Null. Unposted. |

10433 did not save GL 6510100. The 2026-10-05 16:19 comment says it was "coded to Contract Labor (GL 6510100)". The line's `Purchase_GL_Account` is null. 10436's comment does not name an account; it asks Treyce to confirm the GL at post. The line is null.

## Exact field diff

Same 55 value keys on every `APInvoiceLine`. Compared item 53 on blank bill 10452 line 21635 with posted bill 10434 line 21597, and item 46 on blank bill 10458 line 21645 with posted bill 10417 line 21562. Amounts, description text, invoice link, and timestamps differ because they are different invoices. The GL-related difference is only these two lookups:

| Field | Blank API line (unposted) | Posted line after a person posted |
| --- | --- | --- |
| `MFG_Miscellaneous_Item` | set, e.g. `{id: 53, text: "Machining Tools-."}` | same id when the item matches |
| `Purchase_GL_Account` | **null** | `{id, text}` e.g. `{id: 209, text: "5079100 - Machining Tools"}` |
| `Purchase_Discount_GL_Account` | **null** | filled at the same post. Item 53 and item 31 get `{id: 153, text: "5075100 - Purchase Discounts"}`. Item 46, 47, 28, and 13 get the same account as the purchase GL. |
| `Purchase_Variance_GL_Account` | null | null |
| `Invoice_Number_$_Posted` | null | true |
| `ModifierId` / `ModifiedOn` | API Agent at the add | the person who posted, timestamp within about a second of `Posted_Date` |
| `Misc_Description` | set on the 2am lines; **null** on 10433 and 10436 | set |
| `Receipt`, `Part_ID`, `Purchase_Order_Number`, `Purchase_Order_Line` | null | null |
| `PO_Item_Type` | 0 | 0 |
| `Unit_of_Measure` | EA on the October API lines; null on the 2026-09-30 API lines | EA |
| any `GL_Account` / `Expense_GL` key | not present | not present |

Additional-charge rows (`InvoiceAdditionalCharges` on 10356, 10363, 10357, 9732 freight) are a different object. Keys are `Additional_Charges`, `Additional_Charge_Type` (2), `Name`, `Quantity`, `Price`, `Amount`, and invoice links. They have no `Purchase_GL_Account`. Air Products 10356 is six charges, all lookup id 11, name `shop supplies`. That path is not the blank purchase-GL bug.

Representative stored line (blank), bill 10452 line 21635, GL fields and identity only:

```json
{
  "id": 21635,
  "MFG_Miscellaneous_Item": {"id": 53, "text": "Machining Tools-."},
  "Misc_Description": "OSG 1650 No.25 130D WD1 HSSE jobber drill, shipped 4",
  "Quantity": 4.0,
  "Unit_Price": 26.82,
  "Purchase_GL_Account": null,
  "Purchase_Discount_GL_Account": null,
  "Purchase_Variance_GL_Account": null,
  "Unit_of_Measure": {"id": 1, "text": "EA-Each"},
  "Receipt": null,
  "Part_ID": null,
  "PO_Item_Type": 0,
  "Invoice_Number_$_Invoice_Type": 4,
  "Invoice_Number_$_Posted": null,
  "ModifierId": {"id": 175, "text": "API Agent"},
  "ModifiedOn": "2026-10-06T02:17:26.273"
}
```

Same item after a person posted, bill 10434 line 21597:

```json
{
  "id": 21597,
  "MFG_Miscellaneous_Item": {"id": 53, "text": "Machining Tools-."},
  "Purchase_GL_Account": {"id": 209, "text": "5079100 - Machining Tools"},
  "Purchase_Discount_GL_Account": {"id": 153, "text": "5075100 - Purchase Discounts"},
  "Purchase_Variance_GL_Account": null,
  "Invoice_Number_$_Posted": true,
  "ModifierId": {"id": 26, "text": "Kyle Cleaver"},
  "ModifiedOn": "2026-10-05T16:59:50.057"
}
```

10433 line 21618, the bill the 4:15 note describes as already coded to 6510100:

```json
{
  "id": 21618,
  "MFG_Miscellaneous_Item": {"id": 47, "text": "Contract Labor-."},
  "Misc_Description": null,
  "Quantity": 1.0,
  "Unit_Price": 2990.75,
  "Purchase_GL_Account": null,
  "Purchase_Discount_GL_Account": null,
  "Unit_of_Measure": {"id": 1, "text": "EA-Each"},
  "ModifierId": {"id": 175, "text": "API Agent"},
  "ModifiedOn": "2026-10-05T16:19:32.527"
}
```

Posted Luxor 362341 line 20123, for comparison, is item 47, description `SAYTAYPHA, KEVIN`, qty 37.5, price 21.00, `Purchase_GL_Account` id 77 `6510100 - Contract Labor`, discount GL the same account, modified by Treyce at the post time `2026-09-03T11:17:49`.

## The two create paths

### 2am run (and the 2026-09-18 Gas script)

Repo helper since commit `e5db854` (2026-09-18 13:39 UTC), `ap_clerk/misc_lines.py` `misc_add_item_payload`. Callers pass the GL when they have one. The body is:

```json
{
  "id": 10452,
  "state": "Modified",
  "lists": {
    "APInvoiceLine": [{
      "state": "Added",
      "values": {
        "MFG_Miscellaneous_Item": {"id": 53},
        "Misc_Description": "OSG 1650 No.25 130D WD1 HSSE jobber drill, shipped 4",
        "Quantity": 4,
        "Unit_Price": 26.82,
        "Invoice_Number": {"id": 10452},
        "Vendor": {"id": 128},
        "Purchase_GL_Account": {"id": 209}
      }
    }]
  }
}
```

Order on the 2026-10-06 run (`/tmp/ap-run-1006/enter2.py`, not committed; commit `aa3cdee` only stored the workbook): POST the type-4 header, PUT the lines, optional fees or tax, GET readback, then a comment. KIMCO returned HTTP 200 for the line PUT. Readback `Purchase_GL_Account` was null. The only edit retry, bill 10452 line 21635, was:

```json
{
  "id": 10452,
  "state": "Modified",
  "lists": {
    "APInvoiceLine": [{
      "id": 21635,
      "state": "Modified",
      "values": {"Purchase_GL_Account": {"id": 209}}
    }]
  }
}
```

Response: HTTP 400, `Some of the items in this list are not valid`, and on the line `The list does not allow items to be edited`.

The same add-with-GL-id then null readback happened on:

- 2026-09-18 Gas 10135 and 10128 (`Purchase_GL_Account` id 200 in the saved payload, `gl: null` in `after_lines`)
- 2026-10-05 2am Gas 10429–10431 (id 200 sent; 10429's edit retry got the same 400)
- 2026-10-06 UniFirst 10457 (id 57 then id 200), Capital 10458 and 10459 (id 177), RMP 10453 and 10461 (id 209)

2026-10-05 MSC 10434 omitted `gl_account`, so the PUT had no `Purchase_GL_Account` at all. Kyle's later post still wrote 5079100, which is item 53's default. Sending the field and omitting it end in the same stored line. The post, not the add, fills the account.

`ap_clerk/cli.py` is not this path. For a no-PO header it still says not to type Add Item. The live misc lines were added by run scripts calling `misc_add_item_payload`.

### 4:15 poller (2026-10-05 16:19, and the same afternoon pass on 2026-09-30 16:28)

That pass is not in git. `git log -S` and a search of `origin/cursor/ap-run-2026-10-06-live-00ba` do not contain the Luxor/PCT note text or a poller module. It is not one of the cloud agents on this repo between 2026-10-01 and 2026-10-06. What it stored is visible on the lines.

2026-10-05 16:19, after the 2am run had left 10433 and 10436 as headers with no lines:

- 10433: item 47, qty 1, price 2990.75, **description null**, UOM EA, purchase GL null. The note claims GL 6510100 and asks Treyce to confirm or split by worker at post.
- 10436: item 51, qty 1, price 80.00 (the note says 0.50 hours; the line does not), **description null**, UOM EA, purchase GL null. The note does not claim an account.

2026-09-30 16:28, same kind of pass, descriptions were sent:

- RMP 10394 and 10395, MSC 10396 and 10397, PCT 10387. Item set, description set, UOM null, purchase GL null. Notes say "entered as miscellaneous under Machining Tools" or "IT & Computer" and do not say a GL id saved.

Compared with the 2am body, the 4:15 result is missing a stored description on 10433/10436 and is not missing a different GL field. Both paths leave `Purchase_GL_Account` null. The 2am path is the one we can quote, and it already sends the field the posted lines use.

### When it started

Not a later commit that dropped the field. `misc_add_item_payload` has set `Purchase_GL_Account` from `gl_account["id"]` since `e5db854` on 2026-09-18, and the live readback that same day was already null. Human posts before and after that date do store the GL (9732 on 2026-09-01, Luxor through 2026-09-23, Kyle's Gas posts on 2026-09-22 and 2026-10-05, Treyce's RMP posts on 2026-10-02).

The blank lines piled up once the 2am run and the afternoon recode started leaving miscellaneous lines unposted for Treyce. Until someone posts, the purchase GL column stays empty and the API cannot fill it.

## What will not fix the unposted lines

Already tried on live bills. Not repeated for this note.

- Another PUT of `Purchase_GL_Account` on the existing line (`state: "Modified"`). HTTP 400, list does not allow items to be edited. Seen on 10429, 10452, and 10407.
- Deleting the line and adding it again through the API. On 10407, `state: "Removed"` returned HTTP 200 and the three lines were still there, so the re-add never ran.
- Putting the GL on the header. Header accounts (`AP_GL_Account` and the discount/rounding accounts) stay null until post, and they are the AP control accounts, not the line purchase account.
- Sending `Purchase_GL_Account` on the original `state: "Added"` row, with or without `text`, with `Unit_of_Measure`, or in a separate call after the item. The item id sticks. The GL id does not. Doing it in one PUT or as a later PUT does not matter.
- Additional Charges. That list has no purchase-GL property. It would book the charge code's account (F-Fees id 11 on the September shop-supplies bills), which is the wrong account for machining tools, contract labor, and equipment repair.

OPTIONS on invoice 10433 advertises `DELETE, GET, PUT`. PUT is allowed on the invoice record. The child list still rejects an edit of an existing line.

## How to correct the held bills

A person posting from the KIMCO screen is the path that writes `Purchase_GL_Account`. It copies the miscellaneous item's default. That is what happened to the eight bills in the blank list that Kyle posted on 2026-10-05 between 16:57 and 16:59: 10417, 10418, 10419, 10421, 10429, 10430, 10431, and 10434. Those are done. The line GL matches the account the 2am note asked for.

Post these as they stand. The item default is the account you wanted:

- 10452, 10453, 10461 (item 53 → 5079100)
- 10457 (item 28 → 6035100, item 31 → 5081100)
- 10458, 10459 (item 46 → 6455100)
- 10433 (item 47 → 6510100). It is one lump line for $2,990.75 with no worker name. Posted Luxor bills are split by person. Split it in the screen first if it needs to look like 362341. The API cannot edit the line.
- 10396 and 10397 (item 53 → 5079100), if those are still considered open

Do not post these until the item is changed in the screen. The API cannot delete or recode the line.

- **10407.** Item 13 will post to **2599100 Misc Clearing**, as on Capital bill 5153. Kyle wants **6455100**, which is item 46, as on posted bill 9732. Someone has to remove the three lines in the UI and add them as item 46, then post.
- **10436 and 10387.** Item 51 posted to **9999999 Suspense** on PCT 23053. Confirm that is acceptable before posting. If it is not, the line has to be re-added in the UI under an item whose default is the real IT account.

That is the whole held set from this check: seven still-blank bills from the recent list (10452, 10453, 10461, 10457, 10458, 10459, 10407), plus 10433 and 10436 which were described as already saved, plus September leftovers 10396, 10397, and 10387. Eight of the fifteen named blank bills are already posted and have the GL.

## Proposed code fix (do not merge)

No payload field is missing. `misc_add_item_payload` already sends `Purchase_GL_Account`. KIMCO drops it. The code change is to stop treating a null readback as a failed save that a second PUT can repair, and to choose the item whose posted default is the account we want.

```diff
--- a/ap_clerk/misc_lines.py
+++ b/ap_clerk/misc_lines.py
@@
 def misc_add_item_payload(...):
     # Keep sending Purchase_GL_Account. It documents intent.
     # Do not add a second call. state Modified is rejected and
     # state Removed does not delete the line.
@@
+def purchase_gl_saved(line: dict) -> bool:
+    gl = (line.get("values") or line).get("Purchase_GL_Account")
+    return isinstance(gl, dict) and gl.get("id") not in (None, "")
+
+# After the Added PUT, GET the line. If purchase_gl_saved is false,
+# leave the bill unposted and say so. Do not PUT Modified.
+# Do not write "coded to GL nnnnnnn" unless the GET shows that id.
```

Callers (`enter2.py` / `enter3.py` style, and any future poller) should:

1. Map the wanted account to the item whose posted lines actually carry it: 47→6510100 (id 77), 53→5079100 (id 209), 31→5081100 (id 200), 28→6035100 (id 57), 46→6455100 (id 177). Item 13 is 2599100. Item 51 posted as 9999999. Do not use 13 when the account is 6455100.
2. PUT `state: "Added"` once, including `Purchase_GL_Account` as today.
3. GET. If the GL is null, stop. The note should say the item default will post to that account when a person posts, and that the line cannot be edited through the API.
4. Delete the follow-up `state: "Modified"` GL patch. It only produces the 400.

The 4:15 note for 10433 should not say the line was coded to 6510100 until a GET shows id 77.

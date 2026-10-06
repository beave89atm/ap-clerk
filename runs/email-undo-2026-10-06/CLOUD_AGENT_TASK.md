# PHASE 1 ONLY: live READ-ONLY dry run of the 2026-10-06 mail undo (accountspayable@kannonmfg.com)

Branch: `cursor/email-undo-dryrun-2026-10-06` (based on `cursor/ap-sept-catchup-2026-10-06-6aef`, PR 132).

## Inputs on this branch
- `runs/email-undo-2026-10-06/plan.json` / `plan.csv`: 476 rows = 233 from audit tab "Undo plan" (row_id U001-U233) + 243 from tab "Not restored" (N001-N243).
  Built offline from `/workspace/runs/restored-232-audit-2026-10-06.xlsx` plus `runs/sept-catchup-progress.json` (exact `receivedDateTime` to the second) and `qc/sept-catchup-mail-restore.json` (post-restore ids for the 4 timestamp-collision messages).
  `pre_run_folder_id` is the Graph folder id rebuilt from each stale pre-run message id (checked: rebuilds the live Inbox parent id exactly; all 109 FW rows rebuild the known `9 - FORT WORTH ARCHIVE` id).
- `scripts/build_email_undo_plan_2026_10_06.py`: repo-only rebuild of plan.json (verified identical).
- `scripts/email_undo_dryrun_2026_10_06.py`: Graph **GET only**. No KIMCO import. Writes `runs/email-undo-2026-10-06/dryrun.csv`, `summary.md`, `folders.json`.

## Expected end state if phase 2 is later approved (from the plan)
| Destination | Move | Rows |
|---|---|---|
| Inbox | yes (171 FW->Inbox, 4 AutoPay Archive->Inbox) | 175 |
| Inbox | no, category only (171 + Xcaliber category-only) | 172 |
| 9 - FORT WORTH ARCHIVE | yes (restore wrongly pulled to Inbox) | 42 |
| 9 - FORT WORTH ARCHIVE | no, category only | 67 |
| Sent Items | yes | 18 |
| FID 0x0113 folder (U122, Amada marketing) | yes | 1 |
| FID 0x376dc5ed3 folder (N041, Tricor invoice) | yes | 1 |
Categories to clear on every row: `AI Needs Review`, `Entered in AI`, `AI HOLD`, `Entered with issues`, `AI Skipped 2`, `AI Skipped`. Keep every other category.

## Steps
1. `python scripts/email_undo_dryrun_2026_10_06.py` with the saved environment's Graph credentials (env var names only; never print values).
2. Matching: all September mail across all folders, exact `receivedDateTime`, corroborated by subject / sender / invoice #; rows sharing a timestamp with identical plan action are assigned as a set.
3. Statuses: OK_TO_MOVE, CATEGORY_ONLY, ALREADY_DONE, NOT_FOUND, AMBIGUOUS, CONFLICT (destination unresolvable, live folder neither expected-current nor destination, uncorroborated match, or PERSON_TOUCHED: `lastModifiedDateTime` after 8:19 AM CT for catch-up-only rows / after 8:40 AM CT for restored rows, or a follow-up flag set).
4. Commit `dryrun.csv`, `summary.md`, `folders.json` to this branch and push. No message bodies, no secrets.

## Hard limits
Do not move, re-categorize, flag, mark read/unread, delete, send or reply to any message. Do not authenticate to or call KIMCO. Do not modify plan.json.

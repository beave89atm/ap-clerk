"""PHASE 1 DRY RUN for undoing the 2026-10-06 Sept catch-up + restore mail changes on accountspayable@.

READ-ONLY. Graph GET requests only. No KIMCO import, no KIMCO access. No PATCH/POST/DELETE/move/send.
Input:  runs/email-undo-2026-10-06/plan.json  (476 rows: 233 'Undo plan' + 243 'Not restored')
Output: runs/email-undo-2026-10-06/dryrun.csv, summary.md, folders.json
Never prints or writes secrets or message bodies.
"""
from __future__ import annotations
import collections, csv, json, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ap_clerk.graph import ALLOWED_MAILBOX, GRAPH_BASE, GraphClient, load_graph_credentials  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "runs" / "email-undo-2026-10-06"
PLAN = OUT / "plan.json"
REMOVABLE = {"AI Needs Review", "Entered in AI", "AI HOLD", "Entered with issues", "AI Skipped 2", "AI Skipped"}
# Run windows (UTC). Catch-up committed 13:17:46Z (8:17 CT); restore committed 13:38:55Z (8:38 CT).
# The earlier daily AP run committed 07:36:58Z (2:36 CT); the catch-up ran after it.
RUN_START = datetime(2026, 10, 6, 7, 30, tzinfo=timezone.utc)
CATCHUP_END = datetime(2026, 10, 6, 13, 19, 0, tzinfo=timezone.utc)
RESTORE_END = datetime(2026, 10, 6, 13, 40, 0, tzinfo=timezone.utc)
DAY_START = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)  # 10/6 00:00 CT
SELECT = "id,subject,from,receivedDateTime,lastModifiedDateTime,parentFolderId,categories,internetMessageId,flag,isRead"


def get_all(g: GraphClient, url: str, params: dict | None = None) -> list[dict]:
    out, first = [], True
    while url:
        r = g.request("GET", url, params=params if first else None)
        first = False
        if r.status_code != 200:
            raise SystemExit(f"GET failed HTTP {r.status_code} on {url.split('?')[0].replace(GRAPH_BASE, '')}")
        j = r.json() or {}
        out.extend(j.get("value") or [])
        url = j.get("@odata.nextLink")
    return out


def norm(s) -> str:
    s = str(s or "").lower()
    s = re.sub(r"^(re|fw|fwd)\s*:\s*", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def vtoks(s) -> set[str]:
    s = re.sub(r"^\d+\s*-\s*", "", str(s or "").lower())
    stop = {"inc", "llc", "co", "corp", "corporation", "company", "the", "of", "ltd", "lp", "texas", "and", "gp"}
    return {t for t in re.findall(r"[a-z0-9]{3,}", s) if t not in stop}


def parse(ts: str | None):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def main() -> None:
    if not PLAN.exists():  # rebuild offline from repo logs (identical to the committed plan)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from build_email_undo_plan_2026_10_06 import main as build_plan
        OUT.mkdir(parents=True, exist_ok=True)
        PLAN.write_text(json.dumps(build_plan(), indent=1))
    plan = json.loads(PLAN.read_text())
    assert len(plan) == 476, len(plan)
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    g = GraphClient.authenticate(creds.tenant_id, creds.client_id, creds.client_secret)
    base = f"{GRAPH_BASE}/users/{ALLOWED_MAILBOX}"

    # 1) Folder map (all folders incl. hidden), id -> path
    folders: dict[str, dict] = {}
    def walk(url, prefix):
        for f in get_all(g, url, {"$top": 200, "includeHiddenFolders": "true", "$select": "id,displayName,parentFolderId,totalItemCount"}):
            path = f"{prefix}/{f['displayName']}" if prefix else f["displayName"]
            folders[f["id"]] = {"name": f["displayName"], "path": path, "total": f.get("totalItemCount")}
            walk(f"{base}/mailFolders/{quote(f['id'], safe='')}/childFolders", path)
    walk(f"{base}/mailFolders", "")
    # Resolve the pre-run folder ids decoded from the stale message ids (GET mailFolders/{id}, read-only)
    prerun_ids = sorted({p["pre_run_folder_id"] for p in plan})
    resolved = {}
    for fid in prerun_ids:
        r = g.request("GET", f"{base}/mailFolders/{quote(fid, safe='')}", params={"$select": "id,displayName,parentFolderId"})
        if r.status_code == 200:
            j = r.json()
            resolved[fid] = {"live_id": j["id"], "name": j["displayName"], "path": folders.get(j["id"], {}).get("path", j["displayName"])}
        else:
            resolved[fid] = {"error": f"HTTP {r.status_code}"}
    by_name = collections.defaultdict(list)
    for fid, f in folders.items():
        by_name[f["name"]].append(fid)
    (OUT / "folders.json").write_text(json.dumps({"prerun_folder_ids": resolved,
        "named": {n: [folders[i]["path"] for i in ids] for n, ids in by_name.items()
                  if n in ("Inbox", "Sent Items", "9 - FORT WORTH ARCHIVE", "AutoPay Archive")}}, indent=1))

    # 2) Every message in the mailbox received in September CT (all folders)
    msgs = get_all(g, f"{base}/messages", {"$select": SELECT, "$top": 500,
        "$filter": "receivedDateTime ge 2026-09-01T05:00:00Z and receivedDateTime lt 2026-10-01T05:00:00Z"})
    by_time = collections.defaultdict(list)
    for m in msgs:
        by_time[m["receivedDateTime"]].append(m)
    # fallback per-folder for any plan time not seen (hidden folders are not always in /messages)
    missing_times = sorted({p["received_utc"] for p in plan} - set(by_time))
    for t in missing_times:
        for fid in folders:
            for m in get_all(g, f"{base}/mailFolders/{quote(fid, safe='')}/messages",
                             {"$select": SELECT, "$filter": f"receivedDateTime eq {t}", "$top": 50}):
                if m["id"] not in {x["id"] for x in by_time[t]}:
                    by_time[t].append(m)

    def fname(fid):
        return folders.get(fid, {}).get("path", f"UNKNOWN({fid[-16:]})")

    def dest_folder_id(p):
        d = p["planned_destination"]
        if d.startswith("UNNAMED"):
            return resolved.get(p["pre_run_folder_id"], {}).get("live_id")
        ids = by_name.get(d, [])
        # destination must be the same folder the message came from when the plan says so
        live = resolved.get(p["pre_run_folder_id"], {}).get("live_id")
        if live in ids:
            return live
        return ids[0] if len(ids) == 1 else None

    def corroborate(p, m):
        why = []
        subj = m.get("subject") or ""
        logged = [s for s in str(p.get("subjects_logged") or "").split(" | ") if s]
        if logged and any(norm(s) == norm(subj) or (norm(s) and norm(s) in norm(subj)) for s in logged):
            why.append("subject")
        frm = ((m.get("from") or {}).get("emailAddress") or {})
        st = vtoks(p.get("sender_recorded"))
        mt = vtoks(f"{frm.get('name','')} {frm.get('address','').split('@')[-1].split('.')[0]} {frm.get('address','').split('@')[0]}")
        if st and st & mt:
            why.append("sender")
        if st and (st & vtoks(subj)):
            why.append("sender-in-subject")
        for inv in [i for i in str(p.get("invoice_recorded") or "").split(" | ") if len(i) >= 4]:
            if norm(inv) and norm(inv) in norm(subj):
                why.append("invoice-in-subject")
                break
        return why

    # 3) Assign candidates. Rows sharing a received time with identical plan action are assigned as a set.
    groups = collections.defaultdict(list)
    for p in plan:
        groups[p["received_utc"]].append(p)
    results = []
    for t, rows in groups.items():
        cands = by_time.get(t, [])
        assign: dict[str, list[dict]] = {}
        for p in rows:
            hint = [c for c in cands if c["id"] in str(p.get("post_restore_id_hint") or "").split(";")]
            c2 = [c for c in cands if corroborate(p, c)]
            assign[p["row_id"]] = c2 if c2 else cands
            p["_hint"] = hint
        same_plan = len({(p["planned_destination"], p["source_tab"]) for p in rows}) == 1
        for p in rows:
            c = assign[p["row_id"]]
            note = []
            if len(rows) > 1:
                note.append(f"{len(rows)} plan rows share received time {t}")
            if len(rows) > 1 and same_plan and len(c) == len(rows):
                idx = rows.index(p)
                c = [sorted(c, key=lambda x: x["id"])[idx]]
                note.append("interchangeable pair assigned as a set (same destination and action)")
            results.append((p, c, note))

    out_rows = []
    for p, c, note in results:
        st, cur_folder, cur_cats, cur_id, lm, imid, basis, touched = "", "", "", "", "", "", "", ""
        if not c:
            st = "NOT_FOUND"
        elif len(c) > 1:
            st = "AMBIGUOUS"
            note.append("candidates: " + " || ".join(f"{fname(x['parentFolderId'])} / {x.get('subject','')[:60]} / {';'.join(x.get('categories') or [])}" for x in c))
        else:
            m = c[0]
            cur_folder = fname(m["parentFolderId"]); cur_id = m["id"]; imid = m.get("internetMessageId", "")
            cats = m.get("categories") or []; cur_cats = ";".join(cats); lm = m.get("lastModifiedDateTime", "")
            basis = "+".join(["time"] + corroborate(p, m))
            dfid = dest_folder_id(p)
            lmd = parse(lm)
            limit = RESTORE_END if p["source_tab"] == "Undo plan" else CATCHUP_END
            if lmd and lmd > limit:
                touched = f"modified after the run window ({lmd.astimezone(timezone(timedelta(hours=-5))):%H:%M} CT)"
            elif lmd and DAY_START <= lmd < RUN_START:
                note.append("modified 10/6 before the catch-up started")
            flagged = ((m.get("flag") or {}).get("flagStatus") or "").lower() == "flagged"
            if flagged:
                touched = (touched + "; " if touched else "") + "follow-up flag set (run never flags)"
            removable = [x for x in cats if x in REMOVABLE]
            in_dest = bool(dfid) and m["parentFolderId"] == dfid
            exp_cur = p["expected_current_folder"]
            if not dfid:
                st = "CONFLICT"; note.append(f"destination folder '{p['planned_destination']}' not resolvable live")
            elif touched:
                st = "CONFLICT"; note.append("PERSON_TOUCHED: " + touched)
            elif basis == "time":
                st = "CONFLICT"; note.append("unique timestamp match but sender/subject/invoice not corroborated")
            elif in_dest:
                st = "CATEGORY_ONLY" if removable else "ALREADY_DONE"
            elif folders.get(m["parentFolderId"], {}).get("name") != exp_cur.split("/")[-1]:
                st = "CONFLICT"; note.append(f"live folder '{cur_folder}' is neither the expected current folder '{exp_cur}' nor the destination")
            else:
                st = "OK_TO_MOVE"
            if set(cats) - REMOVABLE:
                note.append("keeps non-process categories: " + ";".join(sorted(set(cats) - REMOVABLE)))
        out_rows.append({
            "row_id": p["row_id"], "source_tab": p["source_tab"], "received_ct": p["received_ct"], "received_utc": p["received_utc"],
            "sender_recorded": p["sender_recorded"], "subject_recorded": p["subject_recorded"], "invoice_recorded": p["invoice_recorded"],
            "pre_run_folder": p["pre_run_folder"], "pre_run_folder_live_name": resolved.get(p["pre_run_folder_id"], {}).get("path", ""),
            "planned_destination": p["planned_destination"], "planned_move": p["planned_move"],
            "status": st, "match_basis": basis, "candidates": len(c),
            "live_subject": (c[0].get("subject", "") if len(c) == 1 else ""),
            "live_from": (((c[0].get("from") or {}).get("emailAddress") or {}).get("address", "") if len(c) == 1 else ""),
            "current_folder": cur_folder, "current_categories": cur_cats,
            "categories_to_clear": ";".join(x for x in cur_cats.split(";") if x in REMOVABLE),
            "last_modified_utc": lm, "person_touched": touched, "internet_message_id": imid, "current_id": cur_id,
            "notes": " | ".join(note),
        })
    with open(OUT / "dryrun.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0])); w.writeheader(); w.writerows(out_rows)

    S = collections.Counter(r["status"] for r in out_rows)
    D = collections.Counter((r["planned_destination"], r["status"]) for r in out_rows)
    lines = ["# Email undo dry run 2026-10-06 (PHASE 1, read-only)", "",
             f"Rows: {len(out_rows)} (Undo plan {sum(r['source_tab']=='Undo plan' for r in out_rows)}, Not restored {sum(r['source_tab']=='Not restored' for r in out_rows)})",
             f"September messages listed live: {len(msgs)}", "", "## Counts per status", "", "| Status | Count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(S.items())]
    lines += ["", "## Planned destination x status", "", "| Destination | Status | Count |", "|---|---|---|"]
    lines += [f"| {d} | {s} | {v} |" for (d, s), v in sorted(D.items())]
    lines += ["", "## Pre-run folder ids resolved live", ""]
    lines += [f"- `...{k[-20:]}` -> {v}" for k, v in resolved.items()]
    lines += ["", "## Non-OK rows", "", "| Row | Status | Received CT | Sender | Live subject | Current folder | Categories | Destination | Notes |", "|---|---|---|---|---|---|---|---|---|"]
    for r in out_rows:
        if r["status"] not in ("OK_TO_MOVE", "CATEGORY_ONLY"):
            lines.append(f"| {r['row_id']} | {r['status']} | {r['received_ct']} | {r['sender_recorded']} | {r['live_subject'][:70]} | {r['current_folder']} | {r['current_categories']} | {r['planned_destination']} | {r['notes'][:300]} |")
    lines += ["", "No message was moved, re-categorized, flagged, deleted, sent or replied to. KIMCO was not accessed."]
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"rows": len(out_rows), "status": S}, default=str))


if __name__ == "__main__":
    main()

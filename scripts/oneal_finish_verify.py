"""GET-verify O'Neal finish-out headers. Read-only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.auth import load_credentials
from ap_clerk.kimco import KimcoClient
from oneal_finish_0922 import KNOWN_IDS, snapshot

EXTRA = (10079,)


def main() -> int:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials not ready")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    out = []
    for kid in list(KNOWN_IDS) + list(EXTRA):
        snap = snapshot(client, kid)
        out.append(snap)
        print(
            f"{snap['kimco_id']} {snap['invoice']} batch={snap['batch_id']} "
            f"amt={snap['amount']} ver={snap['verification']} rec={snap['receipt_n']} "
            f"chg={snap['charge_n']}"
        )
    Path("runs/oneal-finish-verify-2026-09-22.json").write_text(
        json.dumps(out, indent=2, default=str) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

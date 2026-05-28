"""
Frequent Freight 2.5 — Pipedrive sync
=====================================
NOTHING is pushed automatically. This script only acts on leads a human has
approved, and only when run explicitly.

Approval comes from one of:
  1. data/approved.json  — a JSON list of approved company_name strings
     (the dashboard's "APPROVE" button exports this; or hand-edit it), OR
  2. each lead in leads.json having "approved": true.

Per approved lead the workflow is exactly:
  1. Create the company as an ORGANIZATION (name, address, phone).
  2. Create the decision-maker as a PERSON/contact (direct email + phone),
     linked to that org.
  3. Add a NOTE on the org with the company description + freight reason + pitch.
  4. Mark the decision-maker as the org's PRIMARY contact.

Usage:
  export PIPEDRIVE_API_TOKEN=xxxx
  export PIPEDRIVE_DOMAIN=yourcompany     # the 'yourcompany' in yourcompany.pipedrive.com
  python pipedrive_push.py --approved-only          # real push
  python pipedrive_push.py --approved-only --dry-run  # print actions, push nothing
"""

import os
import sys
import json
import time
import logging
import argparse
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pipedrive")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LEADS_PATH = DATA_DIR / "leads.json"
APPROVED_PATH = DATA_DIR / "approved.json"

TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
DOMAIN = os.environ.get("PIPEDRIVE_DOMAIN", "")
API = f"https://{DOMAIN}.pipedrive.com/api/v1" if DOMAIN else ""


def _req(method: str, path: str, payload: dict, dry: bool):
    if dry:
        log.info(f"[DRY] {method} {path} :: {json.dumps(payload)[:200]}")
        return {"data": {"id": 0}}
    if not TOKEN or not DOMAIN:
        log.error("PIPEDRIVE_API_TOKEN and PIPEDRIVE_DOMAIN must be set.")
        sys.exit(1)
    url = f"{API}{path}?api_token={TOKEN}"
    r = requests.request(method, url, json=payload, timeout=20)
    if r.status_code >= 300:
        log.error(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
    return r.json()


def create_organization(lead: dict, dry: bool) -> int:
    body = {
        "name": lead["company_name"],
        "address": lead.get("address", ""),
        # phone on org via custom field is account-specific; we keep phone in the
        # org name note + on the person. Pipedrive orgs accept "address" natively.
    }
    res = _req("POST", "/organizations", body, dry)
    org_id = res["data"]["id"]
    log.info(f"  org #{org_id} <- {lead['company_name']}")
    return org_id


def create_person(lead: dict, org_id: int, dry: bool) -> int:
    name = lead.get("dm_name") or f"{lead['company_name']} (decision maker)"
    body = {
        "name": name,
        "org_id": org_id,
        "email": [{"value": lead.get("dm_email", ""), "primary": True, "label": "work"}]
                 if lead.get("dm_email") else [],
        "phone": [{"value": lead.get("dm_phone") or lead.get("phone", ""),
                   "primary": True, "label": "work"}],
        "job_title": lead.get("dm_title", ""),
    }
    res = _req("POST", "/persons", body, dry)
    pid = res["data"]["id"]
    log.info(f"  person #{pid} <- {name} ({lead.get('dm_title','')})")
    return pid


def set_primary_contact(org_id: int, person_id: int, dry: bool):
    # Pipedrive marks a person as primary org contact via the org's
    # 'primary_contact_id' field (or by being the org's first linked person).
    _req("PUT", f"/organizations/{org_id}", {"primary_contact_id": person_id}, dry)
    log.info(f"  primary contact for org #{org_id} = person #{person_id}")


def add_note(org_id: int, person_id: int, lead: dict, dry: bool):
    content = (
        f"<b>What they do:</b> {lead.get('description','')}<br>"
        f"<b>Why they need LTL/freight:</b> {lead.get('freight_reason','')}<br>"
        f"<b>Estimated revenue:</b> ${lead.get('est_revenue',0):,} "
        f"({lead.get('revenue_basis','')})<br>"
        f"<b>10-sec pitch:</b> {lead.get('pitch','')}<br>"
        f"<b>Company phone:</b> {lead.get('phone','')}"
    )
    _req("POST", "/notes",
         {"content": content, "org_id": org_id, "person_id": person_id}, dry)
    log.info(f"  note added to org #{org_id}")


def load_approved_names() -> set:
    if APPROVED_PATH.exists():
        try:
            return set(json.load(open(APPROVED_PATH)))
        except Exception:
            log.warning("approved.json unreadable; falling back to per-lead 'approved' flag")
    return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--approved-only", action="store_true",
                    help="Required safety flag. Only approved leads are pushed.")
    ap.add_argument("--dry-run", action="store_true", help="Print actions, push nothing.")
    args = ap.parse_args()

    if not args.approved_only:
        log.error("Refusing to run. Pass --approved-only to confirm you reviewed the leads.")
        sys.exit(1)

    if not LEADS_PATH.exists():
        log.error(f"{LEADS_PATH} not found. Run fetch.py first.")
        sys.exit(1)

    data = json.load(open(LEADS_PATH))
    leads = data.get("leads", [])
    approved_names = load_approved_names()

    def is_approved(l):
        return l.get("approved") is True or l["company_name"] in approved_names

    to_push = [l for l in leads if is_approved(l)]
    if not to_push:
        log.error("No approved leads found. Approve in the dashboard "
                  "(writes data/approved.json) or set \"approved\": true in leads.json.")
        sys.exit(1)

    log.info(f"Pushing {len(to_push)} approved lead(s){' [DRY RUN]' if args.dry_run else ''}")
    for l in to_push:
        log.info(f"→ {l['company_name']}")
        org_id = create_organization(l, args.dry_run)
        person_id = create_person(l, org_id, args.dry_run)
        add_note(org_id, person_id, l, args.dry_run)
        set_primary_contact(org_id, person_id, args.dry_run)
        time.sleep(0.4)

    log.info("Done.")


if __name__ == "__main__":
    main()

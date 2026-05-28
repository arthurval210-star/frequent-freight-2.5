# Frequent Freight 2.5

Freight / LTL **shipper-prospect** lead generation for the San Antonio metro.
Finds companies that *ship physical goods* (manufacturers, distributors,
suppliers, fabricators, food/plastics producers, etc.) and therefore likely
need LTL/freight support — and **excludes** logistics, 3PL, broker, freight,
and transportation companies.

## What it does
- Scrapes shipper niches across SA-metro hubs (San Antonio, New Braunfels,
  Seguin, Schertz, Boerne) and validates every address against a list of
  surrounding cities.
- Estimates annual revenue from **real signals** (employee count on site,
  multi-location, category baseline) and keeps only the **$500K–$10M** band.
  Every lead carries a `revenue_basis` so the estimate is never confused with
  verified data.
- Scrapes the company's own site for a **real** email and a real
  decision-maker (Owner / Ops / Warehouse / Shipping / Supply Chain /
  Purchasing / GM). Never invents emails or names — unknowns are blanked and
  flagged.
- Writes a company-specific **10-second pitch** with a pattern interrupt in the
  first 3 seconds.
- Runs a quality gate (revenue band, metro city, address+phone, DM present,
  not-a-logistics-company, short pitch) before anything is shown.

## Files
| file | purpose |
|---|---|
| `fetch.py` | scrape + estimate + pitch → `data/leads.json`, `exports/freight_leads_for_review.csv` |
| `dashboard/index.html` | review table; tick APPROVE → export `approved.json` |
| `pipedrive_push.py` | approval-gated Pipedrive sync (org → contact → note → primary flag) |

## Run
```bash
pip install -r requirements.txt
python fetch.py                       # produces leads.json (NOTHING goes to a CRM)
# open dashboard/index.html, review, tick the ✓ on good leads, click EXPORT approved.json
mv ~/Downloads/approved.json data/    # place it next to leads.json
export PIPEDRIVE_API_TOKEN=xxxx
export PIPEDRIVE_DOMAIN=yourcompany
python pipedrive_push.py --approved-only --dry-run   # preview
python pipedrive_push.py --approved-only             # push approved only
```

## Pipedrive workflow (per approved lead)
1. Company → **organization** (name, address).
2. Decision-maker → **person/contact** (direct email + phone), linked to org.
3. **Note** on the org: description + freight reason + revenue + pitch.
4. Decision-maker set as the org's **primary contact**.

## Tuning
- Add/remove territory cities in `SA_METRO_CITIES`.
- Add/remove shipper niches in `SHIPPER_CATEGORIES` (each has a revenue
  baseline + a default freight reason).
- Exclusion keywords live in `EXCLUSION_TOKENS`.
- Revenue band: `REV_MIN` / `REV_MAX`.

## Honest limits
Free scraping can't reliably return verified revenue or a personal cell for
every owner. This system gives you **estimated** revenue (clearly labeled) and
**real** contact data when the site exposes it; everything missing is flagged
`NEEDS_DM_NAME` / `NEEDS_DM_EMAIL` so a human closes the gap before outreach.
For verified firmographics + direct dials at scale, plug a paid data API
(Apollo, ZoomInfo, PDL) into `scrape_site_contacts` / `estimate_revenue`.

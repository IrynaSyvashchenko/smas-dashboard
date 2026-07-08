#!/usr/bin/env python3
"""
Cloud dashboard refresh (runs in GitHub Actions every 2h, 24/7).

Pulls fresh Meta Ads numbers from the Windsor.ai REST API and rewrites data.json
in place. bookings + creatives are CARRIED OVER from the existing data.json
(they change slowly and are recomputed by the daily morning routine); this job
only refreshes the Meta-side numbers: spend / leads / CPL / CPA / instSpend and
the daily graphs. Runs in seconds so it always completes.

Requires env var WINDSOR_API_KEY (stored as a GitHub Actions secret).
Reads and writes ./data.json in the repo root.
"""
import os, json, datetime, urllib.request, urllib.parse, urllib.error, sys

API_KEY   = os.environ.get("WINDSOR_API_KEY", "").strip()
CONNECTOR = "facebook"                 # Meta Ads (Facebook & Instagram)
ACCOUNT   = "873265084670144"          # ad account to keep (ignore others if present)
DATE_FROM = "2026-06-20"
TODAY     = datetime.date.today().isoformat()
DATA_FILE = "data.json"
TZ        = datetime.timezone(datetime.timedelta(hours=2))   # CEST for the "updated" stamp

# manager -> lead-campaign keyword (must NOT match instagram/inst campaigns)
LEAD_KW = {"Диана": "Prague_Diana", "Таня": "Tanya", "Алиса": "Alissa",
           "Саида": "Saida", "Даша": "Dasha"}

def classify(campaign):
    c = campaign or ""
    is_inst = ("_inst" in c) or ("instagram" in c) or ("Saidu" in c)
    if is_inst:
        if "Prague_Diana" in c:
            return ("inst", "Диана")
        if "Saidu" in c or "instagram" in c:
            return ("inst", "Алиса")          # Saidu traffic belongs to Alisa
        return (None, None)
    for m, kw in LEAD_KW.items():
        if kw in c:
            return ("lead", m)
    return (None, None)                        # legacy Paris/traffic/site campaigns -> ignore

def fetch_meta():
    if not API_KEY:
        sys.exit("ERROR: WINDSOR_API_KEY is not set")
    fields = "date,account_id,campaign,spend,actions_lead"
    last_err = None
    for connector in (CONNECTOR, "all"):   # try Meta connector, then the blended 'all'
        url = ("https://connectors.windsor.ai/%s?api_key=%s&date_from=%s&date_to=%s&fields=%s"
               % (connector, urllib.parse.quote(API_KEY), DATE_FROM, TODAY, fields))
        req = urllib.request.Request(url, headers={"User-Agent": "smas-dashboard-refresh"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                payload = json.load(r)
            rows = payload.get("data") or payload.get("result") or []
            if rows:
                print("connector used:", connector, "| rows:", len(rows))
                return rows
            last_err = "connector '%s' returned 0 rows" % connector
            print(last_err)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = "HTTP %s on connector '%s': %s" % (e.code, connector, body)
            print(last_err)
        except Exception as e:
            last_err = "error on connector '%s': %s" % (connector, e)
            print(last_err)
    sys.exit("ERROR fetching Windsor data -> " + str(last_err))

def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def daterange(start, end):
    s = datetime.date.fromisoformat(start); e = datetime.date.fromisoformat(end)
    out = []; d = s
    while d <= e:
        out.append(d.isoformat()); d += datetime.timedelta(days=1)
    return out

def main():
    rows = fetch_meta()

    # keep only the target ad account if the field is present and matches at least once
    has_acc = any("account_id" in r for r in rows)
    if has_acc:
        matched = [r for r in rows if ACCOUNT in str(r.get("account_id", ""))]
        if matched:
            rows = matched

    with open(DATA_FILE, encoding="utf-8") as f:
        cur = json.load(f)

    lead_spend = {m: {} for m in cur["managers"]}
    lead_leads = {m: {} for m in cur["managers"]}
    inst_spend = {m: 0.0 for m in cur["managers"]}

    for r in rows:
        kind, m = classify(r.get("campaign"))
        if m is None or m not in cur["managers"]:
            continue
        d = str(r.get("date"))[:10]
        sp = round(num(r.get("spend")), 4)
        ld = int(num(r.get("actions_lead")))
        if kind == "inst":
            inst_spend[m] += sp
        else:
            lead_spend[m][d] = lead_spend[m].get(d, 0.0) + sp
            lead_leads[m][d] = lead_leads[m].get(d, 0) + ld

    out = json.loads(json.dumps(cur))
    out["updated"] = datetime.datetime.now(TZ).replace(microsecond=0).isoformat()

    for m, node in out["managers"].items():
        dates = daterange(node["start"], TODAY)
        spend = [round(lead_spend[m].get(d, 0.0), 2) for d in dates]
        leads = [lead_leads[m].get(d, 0) for d in dates]
        cpl = [(round(spend[i] / leads[i], 2) if leads[i] > 0 else None) for i in range(len(dates))]
        tot_s = round(sum(spend), 2); tot_l = sum(leads); book = node.get("bookings") or 0
        node["dates"] = dates; node["spend"] = spend; node["leads"] = leads; node["cpl"] = cpl
        node["totalSpend"] = tot_s; node["totalLeads"] = tot_l
        node["avgCpl"] = round(tot_s / tot_l, 2) if tot_l > 0 else None
        node["cpa"] = round(tot_s / book, 2) if book else None
        node["conv"] = round(book / tot_l * 100, 1) if tot_l > 0 else None
        node["instSpend"] = round(inst_spend[m], 2)
        # bookings*, creatives -> carried over unchanged

    compact = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        f.write(compact)

    print("updated", out["updated"], "| bytes", len(compact))
    for m, node in out["managers"].items():
        print(m, "spend", node["totalSpend"], "leads", node["totalLeads"],
              "book", node["bookings"], "cpa", node["cpa"], "inst", node["instSpend"])

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Cloud dashboard refresh (runs in GitHub Actions every 2h, 24/7).

Refreshes both:
  * Meta ad numbers (spend / leads / CPL / instSpend + daily graphs) via Windsor 'facebook'
  * Bookings (записи: total / 7d / 1d) via Windsor 'googlesheets' (manager tabs)

Everything is defensive: if the sheet step fails, bookings are carried over from the
existing data.json and the Meta refresh still commits. Bookings are never zeroed on error.
creatives + bookingsOld1d are carried over (bookingsOld1d needs a day-baseline snapshot).

Requires env var WINDSOR_API_KEY (GitHub Actions secret). Reads/writes ./data.json.
"""
import os, json, datetime, urllib.request, urllib.parse, urllib.error, sys

API_KEY   = os.environ.get("WINDSOR_API_KEY", "").strip()
ACCOUNT   = "873265084670144"
DATE_FROM = "2026-06-20"
TODAY     = datetime.date.today().isoformat()
DATA_FILE = "data.json"
TZ        = datetime.timezone(datetime.timedelta(hours=2))

LEAD_KW = {"Диана": "Prague_Diana", "Таня": "Tanya", "Алиса": "Alissa",
           "Саида": "Saida", "Даша": "Dasha"}

# Google Sheet: manager tabs available in Windsor (Даша's manager tab is NOT connected -> carried)
SHEET_ID = "1sOFTQ3NTeEEFrDbUjdl3p7Jhlx-Fk7duQhk43Rikxx8"
MANAGER_GID = {"Диана": "1178192251", "Таня": "1053387771",
               "Алиса": "36427361", "Саида": "2065248461"}
STATUS_FIELDS = ["первый_звонок", "первое_сообщение", "второй_звонок", "второе_сообщение",
                 "третий_звонок", "третье_сообщение", "третье_сообщение_",
                 "написал_на_whatsapp", "написал_на_whatsapp_1", "написал_на_whatsapp_2", "lead_status"]
SHEET_FIELDS = ["account_id", "phone_number", "created_time", "таргетолог",
                "дата_и_время_записи"] + STATUS_FIELDS

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "smas-dashboard-refresh"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)

# ---------------- META ----------------
def classify(campaign):
    c = campaign or ""
    is_inst = ("_inst" in c) or ("instagram" in c) or ("Saidu" in c)
    if is_inst:
        if "Prague_Diana" in c:
            return ("inst", "Диана")
        if "Saidu" in c or "instagram" in c:
            return ("inst", "Алиса")
        return (None, None)
    for m, kw in LEAD_KW.items():
        if kw in c:
            return ("lead", m)
    return (None, None)

def fetch_meta():
    if not API_KEY:
        sys.exit("ERROR: WINDSOR_API_KEY is not set")
    fields = "date,account_id,campaign,spend,actions_lead"
    last_err = None
    for connector in ("facebook", "all"):
        url = ("https://connectors.windsor.ai/%s?api_key=%s&date_from=%s&date_to=%s&fields=%s"
               % (connector, urllib.parse.quote(API_KEY), DATE_FROM, TODAY, fields))
        try:
            payload = http_json(url)
            rows = payload.get("data") or payload.get("result") or []
            if rows:
                print("meta connector:", connector, "| rows:", len(rows))
                return rows
            last_err = "connector '%s' returned 0 rows" % connector
            print(last_err)
        except urllib.error.HTTPError as e:
            body = ""
            try: body = e.read().decode("utf-8", "replace")[:300]
            except Exception: pass
            last_err = "HTTP %s on '%s': %s" % (e.code, connector, body); print(last_err)
        except Exception as e:
            last_err = "error on '%s': %s" % (connector, e); print(last_err)
    sys.exit("ERROR fetching Meta -> " + str(last_err))

def num(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0

def daterange(start, end):
    s = datetime.date.fromisoformat(start); e = datetime.date.fromisoformat(end)
    out = []; d = s
    while d <= e:
        out.append(d.isoformat()); d += datetime.timedelta(days=1)
    return out

# ---------------- BOOKINGS (Google Sheet) ----------------
def is_booked(r):
    dz = str(r.get("дата_и_время_записи") or "").strip()
    if dz not in ("", "0", "-", "—", "None"):
        return True
    for sf in STATUS_FIELDS:
        if "запис" in str(r.get(sf) or "").lower():
            return True
    return False

def fetch_sheet_rows(gid):
    # Windsor has NO account_id query param; select the tab via a filter on the account_id field.
    fields = urllib.parse.quote(",".join(SHEET_FIELDS), safe=",")
    flt = urllib.parse.quote(json.dumps([["account_id", "eq", "%s-%s" % (SHEET_ID, gid)]]))
    url = ("https://connectors.windsor.ai/googlesheets?api_key=%s&fields=%s&filter=%s"
           % (urllib.parse.quote(API_KEY), fields, flt))
    try:
        payload = http_json(url)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", "replace")[:300]
        except Exception: pass
        raise RuntimeError("HTTP %s: %s" % (e.code, body))
    rows = payload.get("data") or payload.get("result") or []
    same = [r for r in rows if str(r.get("account_id", "")).endswith("-" + gid)]
    return same if same else rows

def compute_bookings():
    """Return {manager: {bookings,bookings7d,bookings1d,leads_seen}} or None on total failure."""
    today = datetime.date.fromisoformat(TODAY)
    res = {}
    for mgr, gid in MANAGER_GID.items():
        try:
            rows = fetch_sheet_rows(gid)
        except Exception as e:
            print("  sheet FAIL", mgr, gid, "->", e); continue
        seen = {}
        for r in rows:
            seen[(str(r.get("phone_number")), str(r.get("created_time")))] = r
        booked = b7 = b1 = leads = 0
        for r in seen.values():
            t = str(r.get("таргетолог") or "").strip().lower()
            if not (("ирина" in t) or t == ""):
                continue
            leads += 1
            if is_booked(r):
                booked += 1
                ct = str(r.get("created_time") or "")[:10]
                try:
                    cd = datetime.date.fromisoformat(ct)
                    if (today - cd).days < 7 and (today - cd).days >= 0: b7 += 1
                    if cd == today: b1 += 1
                except Exception:
                    pass
        res[mgr] = {"bookings": booked, "bookings7d": b7, "bookings1d": b1, "leads_seen": leads}
        print("  sheet %-6s gid %-11s rows %-5d uniq %-4d iryna %-4d | booked %d 7d %d 1d %d"
              % (mgr, gid, len(rows), len(seen), leads, booked, b7, b1))
    return res or None

# ---------------- MAIN ----------------
def main():
    rows = fetch_meta()
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
        sp = round(num(r.get("spend")), 4); ld = int(num(r.get("actions_lead")))
        if kind == "inst":
            inst_spend[m] += sp
        else:
            lead_spend[m][d] = lead_spend[m].get(d, 0.0) + sp
            lead_leads[m][d] = lead_leads[m].get(d, 0) + ld

    out = json.loads(json.dumps(cur))
    out["updated"] = datetime.datetime.now(TZ).replace(microsecond=0).isoformat()

    # Meta-side per manager (bookings carried for now; refined below)
    for m, node in out["managers"].items():
        dates = daterange(node["start"], TODAY)
        spend = [round(lead_spend[m].get(d, 0.0), 2) for d in dates]
        leads = [lead_leads[m].get(d, 0) for d in dates]
        cpl = [(round(spend[i] / leads[i], 2) if leads[i] > 0 else None) for i in range(len(dates))]
        tot_s = round(sum(spend), 2); tot_l = sum(leads)
        node["dates"] = dates; node["spend"] = spend; node["leads"] = leads; node["cpl"] = cpl
        node["totalSpend"] = tot_s; node["totalLeads"] = tot_l
        node["avgCpl"] = round(tot_s / tot_l, 2) if tot_l > 0 else None
        node["instSpend"] = round(inst_spend[m], 2)

    # Bookings from the sheet (defensive: keep carried values on any problem)
    try:
        bk = compute_bookings()
    except Exception as e:
        print("BOOKINGS step failed entirely -> carrying over:", e); bk = None
    if bk:
        for m, v in bk.items():
            if m in out["managers"] and v["leads_seen"] > 0:
                node = out["managers"][m]
                node["bookings"] = v["bookings"]
                node["bookings7d"] = v["bookings7d"]
                node["bookings1d"] = v["bookings1d"]
                # bookingsOld1d carried (needs day-baseline snapshot)

    # recompute cpa/conv from (possibly updated) bookings + fresh spend/leads
    for m, node in out["managers"].items():
        b = node.get("bookings") or 0; ts = node["totalSpend"]; tl = node["totalLeads"]
        node["cpa"] = round(ts / b, 2) if b else None
        node["conv"] = round(b / tl * 100, 1) if tl > 0 else None

    compact = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        f.write(compact)
    print("updated", out["updated"], "| bytes", len(compact))
    for m, node in out["managers"].items():
        print("%-6s spend %-8s leads %-4s book %-3s cpa %-6s inst %s"
              % (m, node["totalSpend"], node["totalLeads"], node["bookings"], node["cpa"], node["instSpend"]))

if __name__ == "__main__":
    main()

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

# Google Sheet manager tabs. Даша (1406387900) works once her tab is connected in Windsor;
# until then her filtered fetch returns 0 rows and her bookings are safely carried over.
SHEET_ID = "1sOFTQ3NTeEEFrDbUjdl3p7Jhlx-Fk7duQhk43Rikxx8"
MANAGER_GID = {"Диана": "1178192251", "Таня": "1053387771",
               "Алиса": "36427361", "Саида": "2065248461", "Даша": "1406387900"}
STATUS_FIELDS = ["первый_звонок", "первое_сообщение", "второй_звонок", "второе_сообщение",
                 "третий_звонок", "третье_сообщение", "третье_сообщение_",
                 "написал_на_whatsapp", "написал_на_whatsapp_1", "написал_на_whatsapp_2", "lead_status"]
SHEET_FIELDS = ["account_id", "phone_number", "created_time", "таргетолог",
                "дата_и_время_записи"] + STATUS_FIELDS

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "smas-dashboard-refresh"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)

def windsor(connector, fields, dfrom=None, dto=None, flt=None):
    """Generic Windsor REST call."""
    q = "api_key=%s&fields=%s" % (urllib.parse.quote(API_KEY),
                                  urllib.parse.quote(",".join(fields), safe=","))
    if dfrom: q += "&date_from=%s" % dfrom
    if dto:   q += "&date_to=%s"   % dto
    if flt:   q += "&filter=%s"    % urllib.parse.quote(json.dumps(flt))
    payload = http_json("https://connectors.windsor.ai/%s?%s" % (connector, q))
    return payload.get("data") or payload.get("result") or []

def phone9(p):
    d = "".join(c for c in str(p or "") if c.isdigit())
    return d[-9:] if len(d) >= 9 else ""

def acct_ok(r):
    a = str(r.get("account_id", ""))
    return (ACCOUNT in a) if a else True

def rate_metrics(imp, clicks, spend, reach):
    """CTR/CPM/частота рахуємо із сум — це коректно, на відміну від усереднення Meta."""
    return {
        "ctr":  round(clicks / imp * 100, 2) if imp else None,
        "cpm":  round(spend / imp * 1000, 2) if imp else None,
        "freq": round(imp / reach, 2)        if reach else None,
        "impr": int(imp), "clicks": int(clicks),
    }

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
        if "запись" in str(r.get(sf) or "").lower():   # matches "Запись из звонка/WhatsApp"
            return True
    return False

# --- lead quality ------------------------------------------------------------
# Воронка ліда: НЕЦІЛЬОВИЙ -> БЕЗ РЕАКЦІЇ -> ЛІКВІД (вийшов на контакт) -> ЗАПИС
#
# НЕЦІЛЬОВІ  = не те місто / сміттєвий номер / дубль  -> проблема ТАРГЕТИНГУ (гео, аудиторія)
# БЕЗ РЕАКЦІЇ = людина лишила номер, але не прочитала / не взяла слухавку / ігнорує
#               -> це НЕ вина менеджера, це якість трафіку: креатив/офер тягне незацікавлених
# ЛІКВІД      = все інше (діалог, думає, відмова, запис) — людина реально відреагувала
BAD_KW    = ["не из париж", "не из праг", "не з париж", "не з праг", "не из город",
             "номер не существует", "нет ватсап", "нет whatsapp", "повтор номера"]
NORESP_KW = ["не прочитано", "игнор", "не взял", "не отвеч", "не отв",
             "не вышел в диалог", "не вышла в диалог"]

def _statuses(r):
    return " | ".join(str(r.get(sf) or "") for sf in STATUS_FIELDS).lower()

def _last_status(r):
    """Останній непорожній статус = поточний стан ліда."""
    vals = [str(r.get(sf) or "").strip() for sf in STATUS_FIELDS]
    vals = [v for v in vals if v and v not in ("0", "-", "—")]
    return vals[-1].lower() if vals else ""

def is_bad(r):
    # нецільовий — це термінальна позначка, шукаємо в будь-якому статусі
    s = _statuses(r); return any(k in s for k in BAD_KW)

def is_noresp(r):
    # без реакції — дивимось ПОТОЧНИЙ стан (останній статус), бо менеджер міг дотиснути пізніше
    s = _last_status(r); return any(k in s for k in NORESP_KW)

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
        yest = today - datetime.timedelta(days=1)
        month_start = today.replace(day=1)
        booked = b7 = b1 = leads = 0
        bY = bM = 0                         # bookings among leads created yesterday / this month
        old_keys = []                       # keys of booked leads created BEFORE today
        # lead quality per period: leads / нецільові / без реакції (ліквід = leads-bad-noresp)
        Q = {p: {"leads": 0, "bad": 0, "noresp": 0} for p in ("yest", "d7", "month", "all")}
        booked7d_phones = []   # телефони записаних лідів, створених за 7 днів (для CPA креативів)

        for k, r in seen.items():
            t = str(r.get("таргетолог") or "").strip().lower()
            if not (("ирина" in t) or t == ""):
                continue
            leads += 1
            ct = str(r.get("created_time") or "")[:10]
            try:
                cd = datetime.date.fromisoformat(ct)
            except Exception:
                cd = None

            bk = is_booked(r)
            bd = (not bk) and is_bad(r)
            nr = (not bk) and (not bd) and is_noresp(r)

            buckets = ["all"]
            if cd:
                if cd == yest: buckets.append("yest")
                if 0 <= (today - cd).days < 7: buckets.append("d7")
                if cd >= month_start: buckets.append("month")
            for p in buckets:
                Q[p]["leads"] += 1
                if bd: Q[p]["bad"] += 1
                if nr: Q[p]["noresp"] += 1

            if bk:
                booked += 1
                if cd:
                    if 0 <= (today - cd).days < 7:
                        b7 += 1
                        ph = phone9(r.get("phone_number"))
                        if ph: booked7d_phones.append(ph)
                    if cd == yest: bY += 1
                    if cd >= month_start: bM += 1
                    if cd == today:
                        b1 += 1
                    elif cd < today:
                        old_keys.append("%s|%s" % (k[0], k[1]))
                else:
                    old_keys.append("%s|%s" % (k[0], k[1]))   # unknown date -> treat as old

        res[mgr] = {"bookings": booked, "bookings7d": b7, "bookings1d": b1,
                    "bookingsYest": bY, "bookingsMonth": bM,
                    "leads_seen": leads, "old_keys": old_keys, "q": Q,
                    "booked7d_phones": booked7d_phones}
        qa = Q["all"]; ql = qa["leads"] or 1
        liq = qa["leads"] - qa["bad"] - qa["noresp"]
        print("  sheet %-6s iryna %-4d | booked %-3d 7d %-3d yest %-2d | нецільові %d (%.0f%%)  без реакції %d (%.0f%%)  ліквід %d (%.0f%%)"
              % (mgr, leads, booked, b7, bY,
                 qa["bad"],    100.0 * qa["bad"]    / ql,
                 qa["noresp"], 100.0 * qa["noresp"] / ql,
                 liq,          100.0 * liq          / ql))
    return res or None

# ---------------- META: метрики по періодах + свіжі креативи ----------------
# сирі вкладки, які Windsor реально читає (решта не синхронізовані -> CPA креативу = null)
RAW_GID = {"Диана": "1148517845"}

def period_metrics(periods):
    """periods = {key:(dfrom,dto)} -> {manager:{key:{ctr,cpm,freq,...}}}
    Запит БЕЗ розбивки по днях — інакше frequency порахується неправильно."""
    out = {}
    for key, (dfrom, dto) in periods.items():
        try:
            rows = windsor("facebook",
                           ["account_id", "campaign", "spend", "impressions", "clicks", "reach"],
                           dfrom, dto)
        except Exception as e:
            print("  meta period FAIL", key, "->", e); continue
        agg = {}
        for r in rows:
            if not acct_ok(r): continue
            kind, m = classify(r.get("campaign"))
            if kind != "lead" or m is None: continue
            a = agg.setdefault(m, {"imp": 0.0, "clicks": 0.0, "spend": 0.0, "reach": 0.0})
            a["imp"]    += num(r.get("impressions")); a["clicks"] += num(r.get("clicks"))
            a["spend"]  += num(r.get("spend"));       a["reach"]  += num(r.get("reach"))
        for m, a in agg.items():
            out.setdefault(m, {})[key] = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
    return out

def build_creatives(d7from, yest_s, bk):
    """Свіжі креативи за 7 днів, тільки АКТИВНІ (витрати > 0 вчора/сьогодні)."""
    ads = windsor("facebook",
                  ["account_id", "campaign", "adset_name", "ad_name", "spend",
                   "impressions", "clicks", "reach", "actions_lead"], d7from, TODAY)
    recent = windsor("facebook",
                     ["account_id", "campaign", "adset_name", "ad_name", "spend"], yest_s, TODAY)
    active = set()
    for r in recent:
        if acct_ok(r) and num(r.get("spend")) > 0:
            active.add((r.get("campaign"), r.get("adset_name"), r.get("ad_name")))

    raw_map = {}   # phone9 -> (manager, ad_name)  із сирих вкладок
    for mgr, gid in RAW_GID.items():
        try:
            for r in windsor("googlesheets", ["account_id", "phone_number", "ad_name"],
                             flt=[["account_id", "eq", "%s-%s" % (SHEET_ID, gid)]]):
                ph = phone9(r.get("phone_number"))
                if ph: raw_map[ph] = (mgr, r.get("ad_name"))
        except Exception as e:
            print("  raw sheet FAIL", mgr, "->", e)

    agg = {}
    for r in ads:
        if not acct_ok(r): continue
        kind, m = classify(r.get("campaign"))
        if kind != "lead" or m is None: continue
        key = (r.get("campaign"), r.get("adset_name"), r.get("ad_name"))
        if key not in active: continue
        a = agg.setdefault(key, {"m": m, "spend": 0.0, "leads": 0,
                                 "imp": 0.0, "clicks": 0.0, "reach": 0.0})
        a["spend"] += num(r.get("spend")); a["leads"] += int(num(r.get("actions_lead")))
        a["imp"]   += num(r.get("impressions")); a["clicks"] += num(r.get("clicks"))
        a["reach"] += num(r.get("reach"))

    out = []
    for (camp, adset, ad), a in agg.items():
        m = a["m"]; sp = round(a["spend"], 2); ld = a["leads"]
        rm = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
        book = None
        if m in RAW_GID and bk and m in bk:
            book = 0
            for ph in bk[m].get("booked7d_phones", []):
                hit = raw_map.get(ph)
                if hit and hit[0] == m and hit[1] == ad:
                    book += 1
        out.append({"m": m, "name": ad, "adset": adset, "campaign": camp,
                    "spend": sp, "leads": ld,
                    "cpl":  round(sp / ld, 2) if ld else None,
                    "book": book,
                    "cpa":  round(sp / book, 2) if book else None,
                    "conv": round(book / ld * 100, 1) if (book is not None and ld) else None,
                    "ctr": rm["ctr"], "freq": rm["freq"], "cpm": rm["cpm"]})
    out.sort(key=lambda c: (c["cpa"] if c["cpa"] is not None else 1e9))
    print("  creatives: %d активних" % len(out))
    return out

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
        # day-baseline snapshot kept inside data.json under "_baseline" (site ignores it).
        # bookingsOld1d = booked-old leads that appeared since the first run of today.
        baseline = cur.get("_baseline") or {}
        base_same_day = (baseline.get("date") == TODAY)
        base_old = baseline.get("old") or {}
        new_base_old = dict(base_old)   # carry entries for managers we don't refresh this run
        for m, v in bk.items():
            if m in out["managers"] and v["leads_seen"] > 0:
                node = out["managers"][m]
                node["bookings"] = v["bookings"]
                node["bookings7d"] = v["bookings7d"]
                node["bookings1d"] = v["bookings1d"]
                node["bookingsYest"] = v["bookingsYest"]
                node["bookingsMonth"] = v["bookingsMonth"]
                node["q"] = v["q"]                      # lead quality per period
                old_now = set(v["old_keys"])
                if base_same_day and (m in base_old):
                    node["bookingsOld1d"] = len(old_now - set(base_old[m]))
                    new_base_old[m] = base_old[m]          # keep the morning baseline unchanged
                else:
                    node["bookingsOld1d"] = 0              # first snapshot of the day for this mgr
                    new_base_old[m] = sorted(old_now)
        out["_baseline"] = {"date": TODAY, "old": new_base_old}

    # --- Meta-метрики по періодах (CTR / CPM / частота) + свіжі креативи ---
    td = datetime.date.fromisoformat(TODAY)
    yest_s = (td - datetime.timedelta(days=1)).isoformat()
    d7from = (td - datetime.timedelta(days=6)).isoformat()
    mstart = td.replace(day=1).isoformat()
    try:
        pm = period_metrics({"yest": (yest_s, yest_s), "d7": (d7from, TODAY),
                             "month": (mstart, TODAY), "all": (DATE_FROM, TODAY)})
        for m, node in out["managers"].items():
            if m in pm:
                node["m"] = pm[m]
    except Exception as e:
        print("period metrics failed -> skipping:", e)

    try:
        cr = build_creatives(d7from, yest_s, bk)
        if cr:
            out["creatives"] = cr
            out["creativesPeriod"] = "останні 7 днів, тільки активні"
    except Exception as e:
        print("creatives failed -> carrying over old ones:", e)

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

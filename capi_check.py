# -*- coding: utf-8 -*-
"""Діагностика охоплення лідів CAPI (Lead Coverage ~30% замість ≥60%).

Нічого не відправляє в Meta і нічого не змінює — тільки читає й друкує звіт:
  1. По кожному ад-сету кабінету 1 за останні CHECK_DAYS повних днів (пояс кабінету):
     ліди в Meta (insights) | ліди в сирих вкладках | з них підуть у CAPI | з них уже в capi_sent.json
  2. Вибірка Lead ID, які ми шлемо: чи знає їх Meta (GET /{lead_id}) і чи збігається ад-сет.
  3. Пряме порівняння ID: ліди з Meta (/{ad_id}/leads, якщо токен має доступ) vs наші.

Змінні: META_TOKEN, SHEETS_URL, SHEETS_KEY (як у дашборда), CHECK_DAYS (за замовч. 3).
"""
import os, json, time, random, datetime, urllib.parse, urllib.error
import refresh_dashboard as rd
import capi_push as cp

ACCOUNT   = rd.ACCOUNT
TZ        = datetime.timezone(datetime.timedelta(hours=4))   # кабінет у поясі Дубая
DAYS      = int(os.environ.get("CHECK_DAYS", "3") or 3)
SAMPLE    = 25
LEAD_ACTS = ("lead", "onsite_conversion.lead_grouped", "leadgen.other")


def graph(path, params):
    p = dict(params, access_token=rd.META_TOKEN)
    url = "%s/%s?%s" % (rd.GRAPH_API, path, urllib.parse.urlencode(p))
    try:
        return rd.http_json(url), None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("error") or {}
        except ValueError:
            err = {}
        return None, "HTTP %s: %s" % (e.code, err.get("message") or "")
    except Exception as e:
        return None, str(e)[:150]


def graph_all(path, params):
    out, err = [], None
    data, err = graph(path, params)
    while data:
        out += data.get("data", [])
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        try:
            data = rd.http_json(nxt)
        except Exception as e:
            err = str(e)[:150]
            break
    return out, err


def lead_count(actions):
    acts = {a.get("action_type"): float(a.get("value") or 0) for a in (actions or [])}
    for k in LEAD_ACTS:
        if k in acts:
            return int(acts[k])
    return 0


def main():
    if not rd.META_TOKEN:
        raise SystemExit("META_TOKEN не заданий")
    now = int(time.time())
    today = datetime.datetime.fromtimestamp(now, TZ).date()
    d1, d0 = today - datetime.timedelta(days=1), today - datetime.timedelta(days=DAYS)
    t0 = int(datetime.datetime.combine(d0, datetime.time(), TZ).timestamp())
    t1 = int(datetime.datetime.combine(today, datetime.time(), TZ).timestamp())
    print("Вікно: %s .. %s (пояс кабінету UTC+4), кабінет %s\n" % (d0, d1, ACCOUNT))

    # --- 1. Meta: ліди по ад-сетах -----------------------------------------------------
    rows, err = graph_all("act_%s/insights" % ACCOUNT, {
        "level": "adset", "fields": "adset_id,adset_name,campaign_name,actions",
        "time_range": json.dumps({"since": str(d0), "until": str(d1)}), "limit": "500"})
    if err:
        print("insights FAIL:", err)
    meta = {}
    for r in rows:
        n = lead_count(r.get("actions"))
        if n:
            meta[str(r["adset_id"])] = {"n": n, "name": r.get("adset_name"), "camp": r.get("campaign_name")}

    # --- 2. Наші: сирі вкладки, фільтр capi_push, реєстр ----------------------------
    own = cp.own_adsets()
    rd.fetch_raw_map()
    sent = cp.load_sent()
    raw, seen = {}, set()
    for x in rd.RAW_LEADS:
        if x["tab"].startswith("nb:"):
            continue
        ts = cp.to_ts(x["created"])
        lid = cp.lead_id(x["id"])
        if not ts or not (t0 <= ts < t1):
            continue
        key = lid or ("noid", x["tab"], x["phone"], x["created"])
        if key in seen:
            continue                        # той самий лід у кількох вкладках
        seen.add(key)
        a = raw.setdefault(x["adset_id"] or "?", {"raw": 0, "cab1": 0, "send": 0, "sent": 0, "ids": [], "mgr": set()})
        a["raw"] += 1
        a["mgr"].add(x["m"])
        if x["m"] not in cp.CAB1:
            continue
        a["cab1"] += 1
        if x["adset_id"] in own and lid:
            a["send"] += 1
            a["ids"].append(lid)
            if "initial_lead" in sent.get(str(lid), []):
                a["sent"] += 1

    print("%-48s %6s %6s %6s %6s %6s  %s" % ("ад-сет (кампанія)", "Meta", "сирі", "CAB1", "шлемо", "в реєстрі", "менеджер у сирих"))
    tot = {"meta": 0, "raw": 0, "cab1": 0, "send": 0, "sent": 0}
    for aid in sorted(set(meta) | set(raw), key=lambda k: -(meta.get(k, {}).get("n", 0))):
        m, a = meta.get(aid, {}), raw.get(aid, {"raw": 0, "cab1": 0, "send": 0, "sent": 0, "mgr": set()})
        name = ("%s (%s)" % (m.get("name") or "?", m.get("camp") or "?"))[:48] if m else "[немає в Meta: %s]" % aid
        print("%-48s %6d %6d %6d %6d %6d  %s" % (name, m.get("n", 0), a["raw"], a["cab1"], a["send"], a["sent"],
                                                ",".join(sorted(a["mgr"])) or "-"))
        tot["meta"] += m.get("n", 0)
        for k in ("raw", "cab1", "send", "sent"):
            tot[k] += a[k]
    print("%-48s %6d %6d %6d %6d %6d" % ("РАЗОМ", tot["meta"], tot["raw"], tot["cab1"], tot["send"], tot["sent"]))
    if tot["meta"]:
        print("частка лідів Meta, для яких шлемо initial_lead: %.0f%%\n" % (100.0 * tot["send"] / tot["meta"]))

    # --- 3. Чи знає Meta наші Lead ID ---------------------------------------------------
    ids = [lid for a in raw.values() for lid in a.get("ids", [])]
    pick = random.sample(ids, min(SAMPLE, len(ids)))
    ok, errs = 0, {}
    for lid in pick:
        d, err = graph(str(lid), {"fields": "id,created_time,adset_id,form_id"})
        if d and str(d.get("id")) == str(lid):
            ok += 1
        else:
            errs[err or "інша відповідь"] = errs.get(err or "інша відповідь", 0) + 1
    print("Вибірка наших Lead ID: %d з %d Meta віддає як лід" % (ok, len(pick)))
    for e, n in errs.items():
        print("   %d × %s" % (n, e))

    # --- 4. Пряме порівняння ID (потрібен доступ leads_retrieval) -----------------------
    ads, err = graph_all("act_%s/ads" % ACCOUNT, {"fields": "id,adset_id,effective_status", "limit": "500",
                                                 "filtering": json.dumps([{"field": "adset.id", "operator": "IN",
                                                                           "value": list(meta)[:50]}])})
    if err:
        print("\nсписок оголошень FAIL:", err)
        return
    mine = set(ids)
    got, miss, fail = 0, [], None
    for ad in ads:
        leads, err = graph_all("%s/leads" % ad["id"], {"fields": "id,created_time", "limit": "500",
                                                       "filtering": json.dumps([{"field": "time_created", "operator": "GREATER_THAN", "value": t0}])})
        if err:
            fail = err
            break
        for l in leads:
            ts = cp.to_ts(l.get("created_time"))
            if ts and t0 <= ts < t1:
                got += 1
                if int(l["id"]) not in mine:
                    miss.append((l["id"], ad["adset_id"], l.get("created_time")))
    if fail:
        print("\n/{ad_id}/leads недоступний для цього токена: %s" % fail)
        return
    print("\nЛіди з Meta по ID: %d, із них НЕ шлемо: %d" % (got, len(miss)))
    for lid, aid, ct in miss[:30]:
        print("   %s  ад-сет %s  %s  %s" % (lid, aid, ct, meta.get(str(aid), {}).get("name", "")))


if __name__ == "__main__":
    main()

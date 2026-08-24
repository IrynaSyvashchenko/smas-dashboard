#!/usr/bin/env python3
"""
Дашборд №2: кабінет ad5 (act_1483053046124984) — Стомат Київ (вініри) + СМАС Відень
(+ майбутні СМАС Київ / Тернопіль). Окремий конвеєр, той самий принцип, що й
refresh_dashboard.py: Meta Graph API (METAТОКЕN) + Google-таблиця «Лиды Анна Олеговна»
через Apps Script-міст. Пише vena/data.json.

Ірина працює з цим кабінетом З 15.08.2026 — ліди/записи до цієї дати належать
попередньому таргетологу і НЕ рахуються (відсікаємо по created_time ліда).

Env vars (ті самі GitHub Secrets, що й у головного дашборда):
  META_TOKEN   -> Meta Marketing API (токен користувача Ірини; має бачити act_1483053046124984)
  SHEETS_URL   -> Apps Script Web App (міст читає будь-яку таблицю, до якої Ірина має доступ)
  SHEETS_KEY   -> спільний секрет моста
"""
import os, re, json, time, datetime, urllib.request, urllib.parse, urllib.error

def _clean_secret(name):
    v = os.environ.get(name, "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v

SHEETS_URL = _clean_secret("SHEETS_URL")
if SHEETS_URL.endswith("/exec/"):
    SHEETS_URL = SHEETS_URL[:-1]
SHEETS_KEY = _clean_secret("SHEETS_KEY")
# Кабінет ad5 бачить профіль Ірини -> окремий токен META_TOKEN_VENA.
# Fallback на META_TOKEN (Анатолій) — на майбутнє, коли буде один спільний токен.
META_TOKEN = _clean_secret("META_TOKEN_VENA") or _clean_secret("META_TOKEN")
GRAPH_API  = "https://graph.facebook.com/v21.0"
ACCOUNT    = "1483053046124984"          # кабінет ad5 (Анна Олеговна)
DATE_FROM  = "2026-08-15"                # старт Ірини в цьому кабінеті
TZ         = datetime.timezone(datetime.timedelta(hours=3))   # Київ (EEST)
TODAY      = datetime.datetime.now(TZ).date().isoformat()
DATA_FILE  = "vena/data.json"

# ---- напрямки (в термінах дашборда — «менеджери») -------------------------
# classify() іде по порядку: Відень ПЕРЕД «смас», бо «15.08_Vienna_smas» містить обидва.
LEAD_KW = {
    "Стомат Київ": ("stomatologia", "viniry", "vinir", "стомат", "quiz"),
    "Відень":      ("vienna", "wien", "відень", "вена"),
    "Тернопіль":   ("ternopil", "тернопіл"),
    "СМАС Київ":   ("smas", "смас"),
}
CITY_OF = {"Стомат Київ": "Київ", "Відень": "Відень",
           "Тернопіль": "Тернопіль", "СМАС Київ": "Київ"}

# ---- Google-таблиця «Лиды Анна Олеговна» ----------------------------------
SHEET_ID = "1RdXP96bS0e6UPnPrkql4z4n21cNV7FbMuk6fdWYHnPA"
# CRM-вкладки (статуси адміна) і сирі вкладки (атрибуція лід -> ad_id) по напрямках.
# Нові напрямки (СМАС Київ / Тернопіль): допиши сюди назви вкладок, коли з'являться.
# «Стомат Квиз Ирина» — вкладка адмінів зі статусами квіз-лідів (телефони/дати
# тягнуться ARRAYFORMULA з сирої «Квіз Ирина»; саму сиру НЕ читаємо, щоб не задвоїти).
MANAGER_SHEET = {"Стомат Київ": ["Стомат Ирина", "Стомат Квиз Ирина"],
                 "Відень":      ["Вена Ирина"],
                 "СМАС Київ":   ["Smas Киев"],
                 "Тернопіль":   ["Smas Тернополь"]}
RAW_SHEETS    = ["fbS", "fbV",
                 # кандидати «на виріст» — неіснуючі просто не прочитаються (м'яко)
                 "fbS2", "fbV2", "fbT", "fbK", "fb1", "fb2"]

# ---- ROAS: середній чек ----------------------------------------------------
UAH_USD = 0.0239   # ~41.8 грн/$
EUR_USD = 1.17
AVG_CHECK = {"Стомат Київ": (39990, "UAH"),
             "Відень":      (149,   "EUR"),
             "СМАС Київ":   (2499,  "UAH"),
             "Тернопіль":   (2499,  "UAH")}

def check_usd_node(m):
    v = AVG_CHECK.get(m)
    if not v:
        return None
    amt, cur = v
    rate = {"UAH": UAH_USD, "EUR": EUR_USD, "USD": 1.0}[cur]
    return {"amount": amt, "cur": cur, "usd": round(amt * rate, 2)}

STATUS_FIELDS = ["статус", "комментарий", "коментарий", "статус_", "lead_status",
                 "первый_звонок", "первое_сообщение", "второй_звонок", "второе_сообщение",
                 "третий_звонок", "третье_сообщение"]

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vena-dashboard-refresh"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)

def num(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0

def phone9(p):
    d = "".join(c for c in str(p or "") if c.isdigit())
    return d[-9:] if len(d) >= 9 else ""

def _phone_of(r):
    for k in ("phone_number", "phone", "telefonnummer", "номер_телефона", "номер_телефону"):
        if r.get(k):
            return r[k]
    return ""

def _strip_id_prefix(v):
    s = str(v or "").strip()
    if len(s) > 1 and ":" in s[:4]:
        s = s.split(":", 1)[1]
    return s.strip()

def rate_metrics(imp, clicks, spend, reach):
    return {"ctr":  round(clicks / imp * 100, 2) if imp else None,
            "cpm":  round(spend / imp * 1000, 2) if imp else None,
            "freq": round(imp / reach, 2)        if reach else None,
            "impr": int(imp), "clicks": int(clicks)}

def daterange(start, end):
    s = datetime.date.fromisoformat(start); e = datetime.date.fromisoformat(end)
    out = []; d = s
    while d <= e:
        out.append(d.isoformat()); d += datetime.timedelta(days=1)
    return out

# ---------------- META (Graph API напряму; без Windsor) ----------------
def graph_rows(fields, dfrom, dto):
    level = "ad" if "ad_id" in fields else ("adset" if "adset_id" in fields else "campaign")
    daily = "date" in fields
    api_fields = ["campaign_name", "spend", "impressions", "clicks", "reach", "actions"]
    if level in ("adset", "ad"):
        api_fields += ["adset_id", "adset_name"]
    if level == "ad":
        api_fields += ["ad_id", "ad_name"]
    params = {"level": level, "fields": ",".join(api_fields),
              "time_range": json.dumps({"since": dfrom, "until": dto}),
              "limit": "500", "access_token": META_TOKEN}
    if daily:
        params["time_increment"] = "1"
    url = "%s/act_%s/insights?%s" % (GRAPH_API, ACCOUNT, urllib.parse.urlencode(params))
    out = []
    while url:
        try:
            payload = http_json(url)
        except urllib.error.HTTPError as e:
            body = ""
            try: body = e.read().decode("utf-8", "replace")[:300]
            except Exception: pass
            raise RuntimeError("graph HTTP %s: %s" % (e.code, body))
        for r in payload.get("data", []):
            leads = 0
            for a in (r.get("actions") or []):
                if a.get("action_type") == "lead":
                    leads += int(float(a.get("value") or 0))
            row = {"campaign": r.get("campaign_name"), "spend": r.get("spend"),
                   "impressions": r.get("impressions"), "clicks": r.get("clicks"),
                   "reach": r.get("reach"), "actions_lead": leads,
                   "adset_id": r.get("adset_id"), "adset_name": r.get("adset_name"),
                   "ad_id": r.get("ad_id"), "ad_name": r.get("ad_name")}
            if daily:
                row["date"] = r.get("date_start")
            out.append(row)
        url = (payload.get("paging") or {}).get("next")
    return out

def meta_rows(fields, dfrom, dto):
    if not META_TOKEN:
        raise RuntimeError("META_TOKEN не заданий")
    return graph_rows(fields, dfrom, dto)

def classify(campaign):
    cl = str(campaign or "").lower()
    if not cl:
        return (None, None)
    for m, kws in LEAD_KW.items():
        if any(k in cl for k in kws):
            return ("lead", m)
    return (None, None)

def fetch_meta():
    """Денні spend/leads по кампаніях. None = Meta недоступна (старі дані лишаються)."""
    try:
        rows = graph_rows(["date", "campaign", "spend", "actions_lead"], DATE_FROM, TODAY)
        print("meta: GRAPH API | rows:", len(rows))
        return rows
    except Exception as e:
        print("meta FAIL (перевір, що META_TOKEN бачить act_%s):" % ACCOUNT, str(e)[:300])
        return None

def _graph_all(edge, fields, extra=None):
    params = {"fields": fields, "limit": "200", "access_token": META_TOKEN}
    if extra:
        params.update(extra)
    url = "%s/act_%s/%s?%s" % (GRAPH_API, ACCOUNT, edge, urllib.parse.urlencode(params))
    out = []
    while url:
        payload = http_json(url)
        out += payload.get("data", [])
        url = (payload.get("paging") or {}).get("next")
    return out

# ---------------- Google-таблиця через міст ----------------
_SHEET_CACHE = {}

def _norm_header(h):
    return str(h).strip().lower().replace(" ", "_")

def fetch_sheet_rows(sheet):
    ck = (SHEET_ID, sheet)
    if ck in _SHEET_CACHE:
        return _SHEET_CACHE[ck]
    if not SHEETS_URL or not SHEETS_KEY:
        raise RuntimeError("SHEETS_URL / SHEETS_KEY не задані")
    url = "%s?key=%s&ss=%s&sheet=%s" % (SHEETS_URL, urllib.parse.quote(SHEETS_KEY),
                                        SHEET_ID, urllib.parse.quote(sheet))
    payload = None; last_err = None
    for _att in range(3):
        try:
            payload = http_json(url); break
        except urllib.error.HTTPError as e:
            body = ""
            try: body = e.read().decode("utf-8", "replace")[:300]
            except Exception: pass
            last_err = RuntimeError("HTTP %s: %s" % (e.code, body))
        except Exception as e:
            last_err = e
        time.sleep(4)
    if payload is None:
        raise last_err
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError("bridge: %s" % payload["error"])
    headers = [_norm_header(h) for h in (payload.get("headers") or [])]
    rows = []
    for raw in (payload.get("rows") or []):
        d = {}
        for i, h in enumerate(headers):
            if h and i < len(raw):
                if h in d and str(raw[i] or "").strip() == "":
                    continue
                d[h] = raw[i]
        rows.append(d)
    _SHEET_CACHE[ck] = rows
    return rows

# ---------------- записи ----------------
def _book_date_cell(r):
    for k, v in r.items():
        if "дата_и_время_записи" in str(k) or "дата_і_час_запису" in str(k):
            s = str(v or "").strip()
            if s not in ("", "0", "-", "—", "None"):
                return s
    return ""

def is_booked(r):
    # Правило Ірини (21.08): запис = заповнена «дата_и_время_записи» АБО слово
    # «запис» у колонці «статус». Коментарі/касания НЕ рахуємо — там «записалася
    # до знайомих», «запишеться після відпустки» тощо давали завищення.
    if _book_date_cell(r):
        return True
    for sf in ("статус", "статус_", "lead_status"):
        s = str(r.get(sf) or "").lower()
        if "отказ" in s or "відмов" in s:
            continue
        for n in ("не запис", "без запис", "уже запис"):
            s = s.replace(n, "")
        if "запис" in s:
            return True
    return False

BAD_KW    = ["не из киев", "не з киев", "не з києв", "не из город", "не то место",
             "случайно", "номер не существует", "нет ватсап", "нет whatsapp",
             "повтор номера", "не из вены", "не з відня", "другой город", "інше місто"]
NORESP_KW = ["не прочитано", "игнор", "ігнор", "не взял", "не взяла", "не отвеч", "не відповід",
             "не вышел в диалог", "не вышла в диалог", "недозвон"]

def _statuses(r):
    return " | ".join(str(r.get(sf) or "") for sf in STATUS_FIELDS).lower()

def _last_status(r):
    vals = [str(r.get(sf) or "").strip() for sf in STATUS_FIELDS]
    vals = [v for v in vals if v and v not in ("0", "-", "—")]
    return vals[-1].lower() if vals else ""

def is_bad(r):
    s = _statuses(r); return any(k in s for k in BAD_KW)

def is_noresp(r):
    s = _last_status(r); return any(k in s for k in NORESP_KW)

def fetch_raw_index():
    """Сирі вкладки (fbS/fbV/...) -> phone9: {m, ct, ad_id, adset_id, ad, adset}.
    Використовується і для атрибуції ад-сетів/креативів, і щоб датувати рядки
    CRM-вкладок (у них може не бути created_time). Тільки кампанії з classify()."""
    idx, stats = {}, {}
    for name in RAW_SHEETS:
        try:
            rows = fetch_sheet_rows(name)
        except Exception as e:
            print("  сира вкладка", name, "->", str(e)[:60]); continue
        n = 0; last = ""
        for r in rows:
            ph = phone9(_phone_of(r))
            if not ph:
                continue
            ct = str(r.get("created_time") or "")[:10]
            if ct and ct > last:
                last = ct
            _k, m = classify(r.get("campaign_name"))
            ad_id = _strip_id_prefix(r.get("ad_id"))
            as_id = _strip_id_prefix(r.get("adset_id"))
            cur = idx.get(ph)
            if cur is None or (ct and ct > cur.get("ct", "")):
                idx[ph] = {"m": m, "ct": ct, "ad_id": ad_id, "adset_id": as_id,
                           "ad": str(r.get("ad_name") or ""),
                           "adset": str(r.get("adset_name") or "")}
            n += 1
        stats[name] = {"rows": n, "last_lead": last}
        print("  сира вкладка %-6s -> %d лідів (останній %s)" % (name, n, last or "?"))
    # Квіз-ліди (вебхук ADSQuiz -> лист «Квіз Ирина»): ad_id немає, атрибуція по
    # НАЗВАХ з UTM (utm_term = ад-сет, utm_content = оголошення). build_adsets/
    # build_creatives підставлять id по назві з інсайтів Meta.
    try:
        rows = fetch_sheet_rows("Квіз Ирина")
        n = 0; last = ""
        for r in rows:
            ph = phone9(_phone_of(r))
            if not ph:
                continue
            ct = str(r.get("created_time") or "")[:10]
            if ct and ct > last:
                last = ct
            cur = idx.get(ph)
            if cur is None or (ct and ct > cur.get("ct", "")):
                idx[ph] = {"m": "Стомат Київ", "ct": ct, "ad_id": "", "adset_id": "",
                           "ad": str(r.get("utm_content") or ""),
                           "adset": str(r.get("utm_term") or ""), "quiz": True}
            n += 1
        stats["Квіз Ирина"] = {"rows": n, "last_lead": last}
        print("  квіз-вкладка       -> %d лідів (останній %s)" % (n, last or "?"))
    except Exception as e:
        print("  квіз-вкладка ->", str(e)[:60])
    return idx, stats

def _tally_manager(rows, today, raw_idx):
    """Метрики однієї CRM-вкладки. Лід Ірини = created_time >= DATE_FROM
    (з рядка або з сирої вкладки по телефону). Без дати = не її, не рахуємо."""
    seen = {}
    for r in rows:
        ph = phone9(_phone_of(r))
        if not ph:
            continue
        ct = str(r.get("created_time") or "")[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}", ct):
            hit = raw_idx.get(ph)
            ct = (hit or {}).get("ct") or ""
        seen[(ph, ct)] = (r, ct)
    yest = today - datetime.timedelta(days=1)
    month_start = today.replace(day=1)
    pm_start = (month_start - datetime.timedelta(days=1)).replace(day=1)
    booked = b7 = b1 = leads = 0
    bY = bM = bPM = 0
    old_keys, booked_keys = [], []
    Q = {p: {"leads": 0, "bad": 0, "noresp": 0} for p in ("yest", "d7", "month", "pmonth", "all")}
    leadsQ = []
    for (ph, ct), (r, _) in seen.items():
        if not ct or ct < DATE_FROM:
            continue                     # лід до 15.08 = попередній таргетолог
        leads += 1
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
            if pm_start <= cd < month_start: buckets.append("pmonth")
        for p in buckets:
            Q[p]["leads"] += 1
            if bd: Q[p]["bad"] += 1
            if nr: Q[p]["noresp"] += 1
        leadsQ.append({"ph": ph, "cd": cd.isoformat() if cd else "", "bk": bk, "bad": bd, "nr": nr})
        if bk:
            booked += 1
            booked_keys.append("%s|%s" % (ph, ct))
            if cd:
                if 0 <= (today - cd).days < 7: b7 += 1
                if cd == yest: bY += 1
                if cd >= month_start: bM += 1
                if pm_start <= cd < month_start: bPM += 1
                if cd == today:
                    b1 += 1
                else:
                    old_keys.append("%s|%s" % (ph, ct))
            else:
                old_keys.append("%s|%s" % (ph, ct))
    return {"bookings": booked, "bookings7d": b7, "bookings1d": b1,
            "bookingsYest": bY, "bookingsMonth": bM, "bookingsPrevMonth": bPM,
            "leads_seen": leads, "old_keys": old_keys, "q": Q,
            "leadsQ": leadsQ, "booked_keys": booked_keys}

def compute_bookings(raw_idx):
    today = datetime.date.fromisoformat(TODAY)
    res = {}
    for mgr, sheets in MANAGER_SHEET.items():
        rows = []
        for sheet in ([sheets] if isinstance(sheets, str) else sheets):
            try:
                rows += fetch_sheet_rows(sheet)
            except Exception as e:
                print("  CRM-вкладка FAIL", mgr, sheet, "->", str(e)[:80])
        if not rows:
            continue
        res[mgr] = _tally_manager(rows, today, raw_idx)
        v = res[mgr]
        print("  CRM %-12s лідів(з 15.08) %-4d | записів %-3d 7д %-3d вчора %d"
              % (mgr, v["leads_seen"], v["bookings"], v["bookings7d"], v["bookingsYest"]))
    return res or None

def _in_period(cd, dfrom, dto):
    return bool(cd) and dfrom <= cd <= dto

# ---------------- ад-сети / креативи по періодах ----------------
def build_adsets(periods, bk, raw_map):
    liveA = ({str(x.get("id")) for x in _graph_all("adsets", "id,effective_status")
              if x.get("effective_status") == "ACTIVE"} if META_TOKEN else None)
    out = {}
    for key, (dfrom, dto) in periods.items():
        try:
            rows = meta_rows(["campaign", "adset_id", "adset_name", "spend",
                              "impressions", "clicks", "reach", "actions_lead"], dfrom, dto)
        except Exception as e:
            print("  ад-сети period FAIL", key, "->", e); continue
        agg = {}
        for r in rows:
            kind, m = classify(r.get("campaign"))
            if m is None: continue
            aid = str(r.get("adset_id") or "")
            if not aid: continue
            a = agg.setdefault(aid, {"m": m, "campaign": r.get("campaign"),
                                     "adset": r.get("adset_name"), "spend": 0.0, "leads": 0,
                                     "imp": 0.0, "clicks": 0.0, "reach": 0.0})
            if r.get("adset_name"): a["adset"] = r.get("adset_name")
            a["spend"] += num(r.get("spend")); a["leads"] += int(num(r.get("actions_lead")))
            a["imp"] += num(r.get("impressions")); a["clicks"] += num(r.get("clicks"))
            a["reach"] += num(r.get("reach"))
        # квіз-ліди без adset_id: садимо на єдиний квіз-адсет (по «quiz» у назві)
        quiz_aids = [a_id for a_id, a_ in agg.items()
                     if "quiz" in str(a_.get("adset") or "").lower()]
        quiz_aid = quiz_aids[0] if len(quiz_aids) == 1 else None
        qmap = {}
        for mgr in (bk or {}):
            for L in (bk.get(mgr, {}) or {}).get("leadsQ", []):
                if not _in_period(L["cd"], dfrom, dto):
                    continue
                hit = raw_map.get(L["ph"])
                if not hit:
                    continue
                aid_h = hit["adset_id"] or (quiz_aid if hit.get("quiz") else "")
                if not aid_h:
                    continue
                q = qmap.setdefault(aid_h, {"leads": 0, "bad": 0, "noresp": 0, "book": 0})
                q["leads"] += 1
                if L["bad"]: q["bad"] += 1
                if L["nr"]:  q["noresp"] += 1
                if L["bk"]:  q["book"] += 1
        res = []
        for aid, a in agg.items():
            sp = round(a["spend"], 2); ld = a["leads"]
            rm = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
            q = qmap.get(aid)
            book = q["book"] if q else None
            res.append({"m": a["m"], "campaign": a["campaign"], "adset": a["adset"],
                        "act": (aid in liveA) if liveA is not None else True,
                        "spend": sp, "leads": ld,
                        "cpl": round(sp / ld, 2) if ld else None,
                        "ctr": rm["ctr"], "freq": rm["freq"], "cpm": rm["cpm"],
                        "book": book,
                        "cpa": round(sp / book, 2) if book else None, "q": q})
        res.sort(key=lambda x: (x.get("act") is False, -(x["spend"] or 0)))
        out[key] = res
        print("  ад-сети [%-6s]: %d рядків" % (key, len(res)))
    return out

def build_creatives(periods, bk, raw_map):
    live = ({str(x.get("id")) for x in _graph_all("ads", "id,effective_status")
             if x.get("effective_status") == "ACTIVE"} if META_TOKEN else None)
    out = {}
    for key, (dfrom, dto) in periods.items():
        try:
            ads = meta_rows(["campaign", "adset_name", "ad_id", "ad_name", "spend",
                             "impressions", "clicks", "reach", "actions_lead"], dfrom, dto)
        except Exception as e:
            print("  creatives period FAIL", key, "->", e); continue
        agg = {}
        for r in ads:
            kind, m = classify(r.get("campaign"))
            if m is None: continue
            aid = str(r.get("ad_id") or "")
            if not aid: continue
            a = agg.setdefault(aid, {"m": m, "campaign": r.get("campaign"),
                                     "adset": r.get("adset_name"), "ad": r.get("ad_name"),
                                     "spend": 0.0, "leads": 0,
                                     "imp": 0.0, "clicks": 0.0, "reach": 0.0})
            if r.get("ad_name"):    a["ad"] = r.get("ad_name")
            if r.get("adset_name"): a["adset"] = r.get("adset_name")
            a["spend"] += num(r.get("spend")); a["leads"] += int(num(r.get("actions_lead")))
            a["imp"] += num(r.get("impressions")); a["clicks"] += num(r.get("clicks"))
            a["reach"] += num(r.get("reach"))
        # квіз-ліди: підбираємо оголошення по «quiz» у назві + збігу utm_content
        def _quiz_ad(hit):
            want = str(hit.get("ad") or "").lower()
            cands = [a_id for a_id, a_ in agg.items()
                     if "quiz" in str(a_.get("ad") or "").lower()
                     and (not want or want in str(a_.get("ad") or "").lower())]
            return cands[0] if len(cands) == 1 else None
        bmap = {}
        for mgr in (bk or {}):
            for L in (bk.get(mgr, {}) or {}).get("leadsQ", []):
                if not L["bk"] or not _in_period(L["cd"], dfrom, dto):
                    continue
                hit = raw_map.get(L["ph"])
                if not hit:
                    continue
                aid_h = hit["ad_id"] or (_quiz_ad(hit) if hit.get("quiz") else None)
                if aid_h:
                    bmap[aid_h] = bmap.get(aid_h, 0) + 1
        res = []
        for aid, a in agg.items():
            sp = round(a["spend"], 2); ld = a["leads"]
            rm = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
            book = bmap.get(aid, 0) if (bk and a["m"] in bk) else None
            res.append({"m": a["m"], "name": a["ad"], "adset": a["adset"], "campaign": a["campaign"],
                        "act": (aid in live) if live is not None else True,
                        "spend": sp, "leads": ld,
                        "cpl": round(sp / ld, 2) if ld else None,
                        "book": book,
                        "cpa": round(sp / book, 2) if book else None,
                        "conv": round(book / ld * 100, 1) if (book is not None and ld) else None,
                        "ctr": rm["ctr"], "freq": rm["freq"], "cpm": rm["cpm"]})
        res.sort(key=lambda c: (not c["act"], c["cpa"] if c["cpa"] is not None else 1e9))
        out[key] = res
        print("  creatives [%-6s]: %d рядків" % (key, len(res)))
    return out

def _local_date(ts):
    s = str(ts or "")
    try:
        if len(s) >= 5 and s[-5] in "+-" and ":" not in s[-5:]:
            s = s[:-2] + ":" + s[-2:]
        return datetime.datetime.fromisoformat(s).astimezone(TZ).date().isoformat()
    except Exception:
        return s[:10]

def _budget_amt(v):
    if isinstance(v, str) and v[:1] in "{[":
        try: v = json.loads(v)
        except Exception: pass
    if isinstance(v, dict):
        for k in ("daily_budget", "lifetime_budget", "budget", "new_value", "old_value", "value", "amount"):
            if v.get(k) is not None:
                v = v[k]; break
    return round(num(v) / 100.0, 2) if num(v) > 0 else None

def fetch_scaling():
    if not META_TOKEN:
        return None
    adsets = _graph_all("adsets", "id,name,daily_budget,effective_status,created_time,"
                                  "campaign{id,name,daily_budget}")
    events = {}
    try:
        for ev in _graph_all("activities", "event_time,event_type,object_id,extra_data",
                             {"since": DATE_FROM, "limit": "500"}):
            et = str(ev.get("event_type") or "").lower()
            if "budget" not in et:
                continue
            oid = str(ev.get("object_id") or "")
            t = str(ev.get("event_time") or "")
            if not oid or (oid in events and t <= events[oid]["t"]):
                continue
            old = new = None
            try:
                x = ev.get("extra_data") or {}
                if isinstance(x, str):
                    x = json.loads(x or "{}")
                old = _budget_amt(x.get("old_value")); new = _budget_amt(x.get("new_value"))
            except Exception:
                pass
            events[oid] = {"t": t, "old": old, "new": new}
    except Exception as e:
        print("  activities FAIL:", str(e)[:120])
    out = []
    for a in adsets:
        if a.get("effective_status") != "ACTIVE":
            continue
        camp = a.get("campaign") or {}
        kind, m = classify(camp.get("name"))
        if m is None:
            continue
        b = a.get("daily_budget"); lvl = "adset"; evt_id = str(a.get("id"))
        if not num(b) and num(camp.get("daily_budget")):
            b = camp.get("daily_budget"); lvl = "campaign"; evt_id = str(camp.get("id"))
        ev = events.get(evt_id)
        chg = None
        if ev:
            chg = {"date": str(ev["t"])[:10], "time": None, "from": ev["old"], "to": ev["new"]}
        out.append({"m": m, "adset": a.get("name"), "campaign": camp.get("name"),
                    "budget": round(num(b) / 100.0, 2) if num(b) else None,
                    "lvl": lvl, "created": _local_date(a.get("created_time")), "chg": chg})
    return out

# ---------------- MAIN ----------------
def main():
    rows = fetch_meta()          # None = Meta недоступна, старі числа лишаються

    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        cur = {"managers": {}}
    cur.setdefault("managers", {})

    # вузли напрямків: створюємо, коли напрямок з'явився в кампаніях кабінету
    seen_dirs = set()
    for r in (rows or []):
        kind, m = classify(r.get("campaign"))
        if m: seen_dirs.add(m)
    for m in seen_dirs:
        if m not in cur["managers"]:
            cur["managers"][m] = {"city": CITY_OF.get(m, ""), "start": DATE_FROM}
            print("новий напрямок у data.json:", m)

    out = json.loads(json.dumps(cur))
    out["updated"] = datetime.datetime.now(TZ).replace(microsecond=0).isoformat()
    out["metaSource"] = "graph" if rows is not None else (cur.get("metaSource") or "—")
    out["periodNote"] = "кабінет ad5 · дані з 15.08 (старт Ірини) · оновлюється кожні 2 год"

    if rows is not None:
        lead_spend = {m: {} for m in out["managers"]}
        lead_leads = {m: {} for m in out["managers"]}
        camp_days = {}
        for r in rows:
            kind, m = classify(r.get("campaign"))
            if m is None or m not in out["managers"]:
                continue
            d = str(r.get("date"))[:10]
            sp = round(num(r.get("spend")), 4); ld = int(num(r.get("actions_lead")))
            lead_spend[m][d] = lead_spend[m].get(d, 0.0) + sp
            lead_leads[m][d] = lead_leads[m].get(d, 0) + ld
            cn = str(r.get("campaign") or "")
            if cn:
                cd_ = camp_days.setdefault(cn, {"m": m, "days": {}})["days"].setdefault(d, [0.0, 0])
                cd_[0] += sp; cd_[1] += ld

        # кампанійний зріз (для звіту)
        _mp = TODAY[:7]
        _pme2 = datetime.date.fromisoformat(TODAY).replace(day=1) - datetime.timedelta(days=1)
        _pp = _pme2.isoformat()[:7]
        _yst = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=1)).isoformat()
        camps = []
        for cn, c in camp_days.items():
            ds = {d: v for d, v in c["days"].items() if v[0] > 0}
            if not ds:
                continue
            alld = sorted(ds)
            def _agg(pref, _ds=ds):
                sp = round(sum(v[0] for d, v in _ds.items() if d.startswith(pref)), 2)
                ld = sum(v[1] for d, v in _ds.items() if d.startswith(pref))
                return {"spend": sp, "leads": ld,
                        "days": sum(1 for d in _ds if d.startswith(pref)),
                        "cpl": round(sp / ld, 2) if ld else None}
            camps.append({"name": cn, "m": c["m"], "start": alld[0], "stop": alld[-1],
                          "active": alld[-1] >= _yst, "month": _agg(_mp), "pmonth": _agg(_pp)})
        camps.sort(key=lambda x: -(x["month"]["spend"] or x["pmonth"]["spend"] or 0))
        out["campaigns"] = camps

        for m, node in out["managers"].items():
            dates = daterange(node["start"], TODAY)
            spend = [round(lead_spend.get(m, {}).get(d, 0.0), 2) for d in dates]
            leads = [lead_leads.get(m, {}).get(d, 0) for d in dates]
            cpl = [(round(spend[i] / leads[i], 2) if leads[i] > 0 else None) for i in range(len(dates))]
            tot_s = round(sum(spend), 2); tot_l = sum(leads)
            node["dates"] = dates; node["spend"] = spend; node["leads"] = leads; node["cpl"] = cpl
            node["totalSpend"] = tot_s; node["totalLeads"] = tot_l
            node["avgCpl"] = round(tot_s / tot_l, 2) if tot_l > 0 else None
            node["check"] = check_usd_node(m)

    # ---- записи з таблиці (м'яко: на помилці старі числа лишаються) ----
    try:
        raw_idx, raw_stats = fetch_raw_index()
    except Exception as e:
        print("raw index FAIL:", e); raw_idx, raw_stats = {}, {}
    try:
        bk = compute_bookings(raw_idx)
    except Exception as e:
        print("BOOKINGS failed -> carrying over:", e); bk = None
    if bk:
        reg = cur.get("_bookReg") or {}
        today_local = datetime.datetime.now(TZ).date().isoformat()
        new_reg = dict(reg)
        for m, v in bk.items():
            if m not in out["managers"]:
                out["managers"][m] = {"city": CITY_OF.get(m, ""), "start": DATE_FROM}
            node = out["managers"][m]
            node["bookings"] = v["bookings"]
            node["bookings7d"] = v["bookings7d"]
            node["bookings1d"] = v["bookings1d"]
            node["bookingsYest"] = v["bookingsYest"]
            node["bookingsMonth"] = v["bookingsMonth"]
            node["bookingsPrevMonth"] = v.get("bookingsPrevMonth", 0)
            node["q"] = v["q"]
            _bd = {}
            for L in v.get("leadsQ", []):
                if L.get("bk") and L.get("cd"):
                    _bd[L["cd"]] = _bd.get(L["cd"], 0) + 1
            node["bookingsDay"] = [_bd.get(d, 0) for d in node.get("dates", [])]
            booked_set = set(v["booked_keys"])
            mreg = dict(reg.get(m) or {})
            migrate = m not in reg
            for kk in booked_set:
                if kk not in mreg:
                    mreg[kk] = "" if migrate else today_local
            mreg = {kk: d for kk, d in mreg.items() if kk in booked_set}
            new_reg[m] = mreg
            node["bookingsToday"] = sum(1 for d in mreg.values() if d == today_local)
        out["_bookReg"] = new_reg

    # ---- періодні метрики + ад-сети/креативи ----
    td = datetime.date.fromisoformat(TODAY)
    yest_s = (td - datetime.timedelta(days=1)).isoformat()
    d7from = (td - datetime.timedelta(days=6)).isoformat()
    mstart = td.replace(day=1).isoformat()
    _pme = td.replace(day=1) - datetime.timedelta(days=1)
    _pms = _pme.replace(day=1)
    PERIODS = {"yest": (yest_s, yest_s), "d7": (d7from, TODAY),
               "month": (mstart, TODAY),
               "pmonth": (_pms.isoformat(), _pme.isoformat()),
               "all": (DATE_FROM, TODAY)}

    if rows is not None:
        try:
            pmx = {}
            for key, (dfrom, dto) in PERIODS.items():
                prow = meta_rows(["campaign", "spend", "impressions", "clicks", "reach"], dfrom, dto)
                agg = {}
                for r in prow:
                    kind, m = classify(r.get("campaign"))
                    if m is None: continue
                    a = agg.setdefault(m, {"imp": 0.0, "clicks": 0.0, "spend": 0.0, "reach": 0.0})
                    a["imp"] += num(r.get("impressions")); a["clicks"] += num(r.get("clicks"))
                    a["spend"] += num(r.get("spend"));     a["reach"] += num(r.get("reach"))
                for m, a in agg.items():
                    pmx.setdefault(m, {})[key] = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
            for m, node in out["managers"].items():
                if m in pmx:
                    node["m"] = pmx[m]
        except Exception as e:
            print("period metrics failed:", e)

        raw_map = {ph: h for ph, h in raw_idx.items()
                   if h.get("ad_id") or h.get("adset_id") or h.get("quiz")}

        def _merge_periods(field, fresh):
            if not fresh:
                print(field, "failed -> carrying over"); return
            prev = out.get(field)
            if isinstance(prev, dict):
                merged = dict(prev); merged.update(fresh); fresh = merged
            out[field] = fresh
        try:
            _merge_periods("creatives", build_creatives(PERIODS, bk, raw_map))
        except Exception as e:
            print("creatives failed:", e)
        try:
            _merge_periods("adsets", build_adsets(PERIODS, bk, raw_map))
        except Exception as e:
            print("adsets failed:", e)

        try:
            sc = fetch_scaling()
            if sc is not None:
                out["scaling"] = sc
        except Exception as e:
            print("scaling failed:", str(e)[:120])

    out["_diag"] = {"rawTabs": raw_stats}

    for m, node in out["managers"].items():
        b = node.get("bookings") or 0
        ts = node.get("totalSpend") or 0; tl = node.get("totalLeads") or 0
        node["cpa"] = round(ts / b, 2) if b and ts else None
        node["conv"] = round(b / tl * 100, 1) if tl > 0 else None

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    compact = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        f.write(compact)
    print("updated", out["updated"], "| bytes", len(compact))
    for m, node in out["managers"].items():
        print("%-14s spend %-8s leads %-4s book %-3s" % (
            m, node.get("totalSpend"), node.get("totalLeads"), node.get("bookings")))

if __name__ == "__main__":
    main()

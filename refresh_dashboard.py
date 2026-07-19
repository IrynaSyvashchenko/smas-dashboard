#!/usr/bin/env python3
"""
Cloud dashboard refresh (runs in GitHub Actions every 2h, 24/7).

Refreshes both:
  * Meta ad numbers (spend / leads / CPL / instSpend + daily graphs) via Windsor 'facebook'
  * Bookings + lead quality via the Apps Script BRIDGE (Google Sheets, read as Iryna)

Google Sheets no longer go through Windsor: Windsor mangled the tab names («фб3» -> cell FB3)
and its trial expires. The bridge is a Web App deployed on Iryna's own account, so it reads
exactly the sheets she already has access to. No sheet is ever made public.

Everything is defensive: if the sheet step fails, bookings are carried over from the
existing data.json and the Meta refresh still commits. Bookings are never zeroed on error.

Env vars (GitHub Actions secrets):
  WINDSOR_API_KEY  -> Meta ads (fallback, поки живий тариф Windsor)
  META_TOKEN       -> токен Meta Marketing API (60 днів); якщо заданий — Meta йде НАПРЯМУ
  SHEETS_URL       -> https://script.google.com/macros/s/.../exec
  SHEETS_KEY       -> shared secret checked by the bridge
Reads/writes ./data.json.
"""
import os, json, datetime, urllib.request, urllib.parse, urllib.error, sys

API_KEY    = os.environ.get("WINDSOR_API_KEY", "").strip()
def _clean_secret(name):
    """Прибираємо пробіли/переноси і випадкові лапки — типова помилка при вставці в GitHub Secrets."""
    v = os.environ.get(name, "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v

SHEETS_URL = _clean_secret("SHEETS_URL")
if SHEETS_URL.endswith("/exec/"):
    SHEETS_URL = SHEETS_URL[:-1]
SHEETS_KEY = _clean_secret("SHEETS_KEY")
META_TOKEN = _clean_secret("META_TOKEN")
GRAPH_API  = "https://graph.facebook.com/v21.0"
ACCOUNT   = "873265084670144"
DATE_FROM = "2026-06-20"
TODAY     = datetime.date.today().isoformat()
DATA_FILE = "data.json"
TZ        = datetime.timezone(datetime.timedelta(hours=2))

LEAD_KW = {"Диана": "Prague_Diana", "Таня": "Tanya", "Алиса": "Alissa",
           "Саида": "Saida", "Даша": "Dasha",
           # Мага ПЕРЕД Юліаною: її РК може містити "Barcelona"
           "Мага": ("Maga", "Мага"),
           "Юлиана": "Barcelona"}

# --- Instagram-кампанії: окремі картки на дашборді -------------------------
# Ліди цих РК ідуть в Instagram Direct — їх немає ні в лід-формах Meta, ні в
# Google-таблиці. Витрати/CTR/частота тягнуться з Meta автоматично, а ліди й
# записи Ірина повідомляє вручну -> INST_MANUAL нижче.
INST_MGR_PARIS  = "Инста Париж"   # РК «на Саиду» (Saidu), Direct веде Алиса
INST_MGR_PRAGUE = "Инста Прага"

# {картка: {дата: {"l": ліди за день, "b": записи за день}}} — правиться руками.
# Инста Париж: ліди Meta рахує САМА (ліди з листування, action=lead) — вручну тільки записи!
# Инста Прага: Meta лідів не бачить (0) — вручну і ліди, і записи.
# Дата 01.07 = «за липень разом, дні невідомі»: рахується в «Липень» і «Весь час»,
# але не у «Вчора»/«7 днів» (чесніше, ніж вигадувати дні).
INST_MANUAL = {
    INST_MGR_PRAGUE: {"2026-07-01": {"l": 36, "b": 10}},   # від Ірини 19.07: за липень 36 лідів / 10 записів
    INST_MGR_PARIS:  {"2026-07-01": {"l": 0, "b": 4}},     # 4 записи за липень (дати невідомі)
}

# Нові менеджери: вузол у data.json створюється автоматично при першому запуску
MANAGER_BOOTSTRAP = {"Юлиана": {"city": "Барселона", "start": "2026-07-09"},
                     "Мага":   {"city": "Париж", "start": "2026-07-18"},
                     INST_MGR_PARIS:  {"city": "Париж", "start": "2026-06-20"},
                     INST_MGR_PRAGUE: {"city": "Прага", "start": "2026-06-20"}}

# --- ROAS: середній чек (записи x чек / витрати; оцінка, бо запис != оплачена процедура) ---
# Курси до USD (валюта кабінету) — константи, онови за потреби.
EUR_USD = 1.17
CZK_USD = 0.047
AVG_CHECK = {   # (сума, валюта). Париж/Барселона — ціна оферу в кампанії; Прага — «модельна» ціна.
    # Диана: з 18.07 акція для моделей 2999 Kč (стара модельна 3990 лишилась у Даші/інсти).
    "Диана": (2999, "CZK"), "Даша": (3990, "CZK"),
    "Таня": (199, "EUR"), "Алиса": (159, "EUR"), "Саида": (159, "EUR"),
    "Юлиана": (199, "EUR"),
    "Мага":   (199, "EUR"),   # Париж; припущення = офер 199€, поправ, якщо інший

    INST_MGR_PARIS: (159, "EUR"), INST_MGR_PRAGUE: (3990, "CZK"),
}

def check_usd_node(m):
    v = AVG_CHECK.get(m)
    if not v:
        return None
    amt, cur = v
    rate = {"EUR": EUR_USD, "CZK": CZK_USD, "USD": 1.0}[cur]
    return {"amount": amt, "cur": cur, "usd": round(amt * rate, 2)}

# Google Sheet manager tabs, read through the Apps Script bridge (gid = tab id in the sheet).
# If a tab can't be read, that manager's fetch fails softly and her bookings are carried over.
SHEET_ID = "1sOFTQ3NTeEEFrDbUjdl3p7Jhlx-Fk7duQhk43Rikxx8"
# Мага з 18.07 переїхала в головну таблицю: CRM-вкладка «Мага Ирина» (як у всіх),
# сира вкладка fb5 (див. RAW_GID) — стара вкладка в барселонській таблиці видалена.
MANAGER_GID = {"Диана": "1178192251", "Таня": "1053387771",
               "Алиса": "36427361", "Саида": "2065248461", "Даша": "1406387900",
               "Мага": "1445117368"}
STATUS_FIELDS = ["первый_звонок", "первое_сообщение", "второй_звонок", "второе_сообщение",
                 "третий_звонок", "третье_сообщение", "третье_сообщение_",
                 "написал_на_whatsapp", "написал_на_whatsapp_1", "написал_на_whatsapp_2", "lead_status"]

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

def graph_rows(fields, dfrom, dto):
    """Meta Marketing API insights -> рядки У ФОРМАТІ WINDSOR (ті самі ключі),
    щоб решта коду не змінювалась. level виводиться з полів; 'date' у полях =
    розбивка по днях (time_increment=1). БЕЗ 'date' — суми за період, тоді
    reach/frequency коректні (грабля: частоту не можна сумувати по днях)."""
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
            row = {"account_id": ACCOUNT, "campaign": r.get("campaign_name"),
                   "spend": r.get("spend"), "impressions": r.get("impressions"),
                   "clicks": r.get("clicks"), "reach": r.get("reach"),
                   "actions_lead": leads,
                   "adset_id": r.get("adset_id"), "adset_name": r.get("adset_name"),
                   "ad_id": r.get("ad_id"), "ad_name": r.get("ad_name")}
            if daily:
                row["date"] = r.get("date_start")
            out.append(row)
        url = (payload.get("paging") or {}).get("next")
    return out

META_SOURCE = {"used": "windsor"}   # що реально спрацювало в цьому запуску (пишеться в data.json)

def meta_rows(fields, dfrom, dto):
    """Одна точка доступу до Meta: Graph API напряму (якщо є META_TOKEN),
    інакше Windsor. Формат рядків однаковий."""
    if META_TOKEN:
        try:
            return graph_rows(fields, dfrom, dto)
        except Exception as e:
            print("  graph FAIL -> windsor:", str(e)[:200])
    return windsor("facebook", fields, dfrom, dto)

def phone9(p):
    d = "".join(c for c in str(p or "") if c.isdigit())
    return d[-9:] if len(d) >= 9 else ""

def acct_ok(r):
    a = str(r.get("account_id", ""))
    return (ACCOUNT in a) if a else True

def rate_metrics(imp, clicks, spend, reach):
    """CTR/CPM/частоту рахуємо із сум — це коректно, на відміну від усереднення Meta."""
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
        if "Prague_Diana" in c or "Prague" in c:
            return ("inst", INST_MGR_PRAGUE)
        return ("inst", INST_MGR_PARIS)
    cl = c.lower()
    for m, kw in LEAD_KW.items():
        kws = kw if isinstance(kw, tuple) else (kw,)
        if any(k.lower() in cl for k in kws):
            return ("lead", m)
    return (None, None)

def fetch_meta():
    if META_TOKEN:
        try:
            rows = graph_rows(["date", "account_id", "campaign", "spend", "actions_lead"],
                              DATE_FROM, TODAY)
            if rows:
                print("meta source: GRAPH API | rows:", len(rows))
                META_SOURCE["used"] = "graph"
                return rows
            print("graph api returned 0 rows -> falling back to windsor")
        except Exception as e:
            print("graph api failed -> falling back to windsor:", str(e)[:300])
    if not API_KEY:
        sys.exit("ERROR: no META_TOKEN worked and WINDSOR_API_KEY is not set")
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
# НЕЦІЛЬОВІ  = не те місто / сміттєвий номер / дубль / випадковий  -> проблема ТАРГЕТИНГУ (гео, аудиторія)
# БЕЗ РЕАКЦІЇ = людина лишила номер, але не прочитала / не взяла слухавку / ігнорує
#               -> це НЕ вина менеджера, це якість трафіку: креатив/офер тягне незацікавлених
# ЛІКВІД      = все інше (діалог, думає, відмова, запис) — людина реально відреагувала
BAD_KW    = ["не из париж", "не из праг", "не з париж", "не з праг", "не из город",
             "не из барсел", "не з барсел", "случайно",
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

def _norm_header(h):
    """«Дата и время записи» -> «дата_и_время_записи» (як це робив Windsor)."""
    return str(h).strip().lower().replace(" ", "_")

def _strip_id_prefix(v):
    """Google Sheets лід-експорт пише 'as:12345', 'ag:12345', 'p:+33...'. Знімаємо префікс."""
    s = str(v or "").strip()
    if len(s) > 1 and ":" in s[:4]:
        s = s.split(":", 1)[1]
    return s.strip()

_SHEET_CACHE = {}

def fetch_sheet_rows(gid=None, ss=None, sheet=None):
    """Читає вкладку через Apps Script-міст (за gid або за НАЗВОЮ вкладки, з
    будь-якої таблиці, до якої Ірина має доступ). Повертає список dict-ів з
    нормалізованими ключами заголовків — тобто те саме, що раніше віддавав Windsor."""
    ss = ss or SHEET_ID
    ck = (ss, gid, sheet)
    if ck in _SHEET_CACHE:
        return _SHEET_CACHE[ck]
    if not SHEETS_URL or not SHEETS_KEY:
        raise RuntimeError("SHEETS_URL / SHEETS_KEY не задані")
    url = "%s?key=%s&ss=%s" % (SHEETS_URL, urllib.parse.quote(SHEETS_KEY), ss)
    if sheet:
        url += "&sheet=%s" % urllib.parse.quote(sheet)
    else:
        url += "&gid=%s" % gid
    try:
        payload = http_json(url)           # Apps Script віддає 302 -> urllib йде за ним сам
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", "replace")[:300]
        except Exception: pass
        raise RuntimeError("HTTP %s: %s" % (e.code, body))
    if isinstance(payload, dict) and payload.get("error"):
        if payload["error"] == "forbidden":
            raise RuntimeError("bridge: forbidden — секрет SHEETS_KEY (%d символів) не збігається "
                               "з const KEY в Apps Script" % len(SHEETS_KEY))
        raise RuntimeError("bridge: %s" % payload["error"])

    headers = [_norm_header(h) for h in (payload.get("headers") or [])]
    rows = []
    for raw in (payload.get("rows") or []):
        d = {}
        for i, h in enumerate(headers):
            if h and i < len(raw):
                d[h] = raw[i]
        rows.append(d)
    _SHEET_CACHE[ck] = rows
    return rows

# ---------------- БАРСЕЛОНА (Юлиана) ----------------
# Окрема таблиця. Сирі вкладки фб3/fb. = атрибуція лідів до ad_id/adset_id
# (для таблиць ад-сетів/креативів). Кампанії Ірини звуться "Yaliana"
# (Вірині — "Yuliana", інша буква, чужий кабінет).
BARCA_SS        = "1yabb6jwu15n8N9CBTdWk3ypzvc6mkldclpAu5nNpVVg"
# ПРАВИЛО ІРИНИ (17.07): її ліди/записи = вкладка «Юлиана Ирина», і тільки вона.
# «Де у моїй вкладці написано запис — то мій запис». Жодної атрибуції по кампаніях
# для записів; вкладка Віри більше не читається.
BARCA_IRYNA_SHEET = "Юлиана Ирина"
BARCA_RAW_SHEETS = ("фб3", "fb.")    # сирі вкладки з ad_id (ліди задвоєні між ними — дедуп по телефону)
BARCA_CAMP_KW   = ("yaliana",)
BARCA_MGR       = "Юлиана"

def _barca_phone(r):
    # телефон лежить то в número_de_teléfono, то в phone_number — беремо що є
    return r.get("número_de_teléfono") or r.get("phone_number")

def fetch_barcelona_raw(extra_camps=frozenset()):
    """{phone9: {m, ad_id, adset_id, ad, adset}} — прив'язка телефону до ad_id/adset_id
    по рядках кампаній Ірини (Yaliana) у сирих вкладках. Використовується ТІЛЬКИ для
    таблиць ад-сетів/креативів; чиї ліди й записи — вирішує вкладка «Юлиана Ирина»."""
    out = {}
    for name in BARCA_RAW_SHEETS:
        for r in fetch_sheet_rows(ss=BARCA_SS, sheet=name):
            camp = str(r.get("campaign_name") or "")
            cl = camp.lower()
            if not (any(k in cl for k in BARCA_CAMP_KW) or (camp and camp in extra_camps)):
                continue
            ph = phone9(_barca_phone(r))
            if not ph:
                continue
            out[ph] = {"m": BARCA_MGR,
                       "ad_id": _strip_id_prefix(r.get("ad_id")),
                       "adset_id": _strip_id_prefix(r.get("adset_id")),
                       "ad": str(r.get("ad_name") or ""),
                       "adset": str(r.get("adset_name") or "")}
    return out

def _iryna_row(r):
    t = str(r.get("таргетолог") or "").strip().lower()
    return ("ирина" in t) or t == ""

def _tally_manager(rows, today, phone_of, row_ok):
    """Акумулює метрики одного менеджера. phone_of(r) -> сирий телефон
    (для дедупу і join), row_ok(r) -> чи рахуємо цей лід як лід Ірини."""
    seen = {}
    for r in rows:
        seen[(str(phone_of(r)), str(r.get("created_time")))] = r
    yest = today - datetime.timedelta(days=1)
    month_start = today.replace(day=1)
    booked = b7 = b1 = leads = 0
    bY = bM = 0                         # bookings among leads created yesterday / this month
    old_keys = []                       # keys of booked leads created BEFORE today
    booked_keys = []                    # ключі ВСІХ поточних записів (для реєстру «коли з'явився запис»)
    # lead quality per period: leads / нецільові / без реакції (ліквід = leads-bad-noresp)
    Q = {p: {"leads": 0, "bad": 0, "noresp": 0} for p in ("yest", "d7", "month", "all")}
    leadsQ = []   # усі мої ліди з телефоном: дата + прапорці (для таблиць ад-сетів/креативів по періодах)

    for k, r in seen.items():
        if not row_ok(r):
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

        ph = phone9(phone_of(r))
        if ph:
            leadsQ.append({"ph": ph, "cd": cd.isoformat() if cd else "",
                           "bk": bk, "bad": bd, "nr": nr})

        if bk:
            booked += 1
            booked_keys.append("%s|%s" % (k[0], k[1]))
            if cd:
                if 0 <= (today - cd).days < 7:
                    b7 += 1
                if cd == yest: bY += 1
                if cd >= month_start: bM += 1
                if cd == today:
                    b1 += 1
                elif cd < today:
                    old_keys.append("%s|%s" % (k[0], k[1]))
            else:
                old_keys.append("%s|%s" % (k[0], k[1]))   # unknown date -> treat as old

    return {"bookings": booked, "bookings7d": b7, "bookings1d": b1,
            "bookingsYest": bY, "bookingsMonth": bM,
            "leads_seen": leads, "old_keys": old_keys, "q": Q,
            "leadsQ": leadsQ, "booked_keys": booked_keys}

def _print_mgr(mgr, v):
    qa = v["q"]["all"]; ql = qa["leads"] or 1
    liq = qa["leads"] - qa["bad"] - qa["noresp"]
    print("  sheet %-6s iryna %-4d | booked %-3d 7d %-3d yest %-2d | нецільові %d (%.0f%%)  без реакції %d (%.0f%%)  ліквід %d (%.0f%%)"
          % (mgr, v["leads_seen"], v["bookings"], v["bookings7d"], v["bookingsYest"],
             qa["bad"],    100.0 * qa["bad"]    / ql,
             qa["noresp"], 100.0 * qa["noresp"] / ql,
             liq,          100.0 * liq          / ql))

def compute_bookings(barca_camps=frozenset()):
    """Return {manager: {bookings,bookings7d,bookings1d,leads_seen}} or None on total failure."""
    today = datetime.date.fromisoformat(TODAY)
    res = {}
    for mgr, gid in MANAGER_GID.items():
        try:
            rows = fetch_sheet_rows(gid)
        except Exception as e:
            print("  sheet FAIL", mgr, gid, "->", e); continue
        res[mgr] = _tally_manager(rows, today,
                                  lambda r: r.get("phone_number"), _iryna_row)
        _print_mgr(mgr, res[mgr])

    # Барселона: ліди і записи Юліани = вкладка «Юлиана Ирина» (правило Ірини:
    # «де у моїй вкладці написано запис — то мій запис»). Без фільтрів по кампаніях.
    try:
        rows = fetch_sheet_rows(ss=BARCA_SS, sheet=BARCA_IRYNA_SHEET)
        res[BARCA_MGR] = _tally_manager(
            rows, today, _barca_phone,
            lambda r: bool(phone9(_barca_phone(r))))
        _print_mgr(BARCA_MGR, res[BARCA_MGR])
    except Exception as e:
        print("  барселона FAIL ->", e)
    return res or None

# ---------------- META: метрики по періодах + свіжі креативи ----------------
# Сирі вкладки (лід -> ad_id / adset_id по телефону). Читаються через міст, не через Windsor.
RAW_GID = {
    "Диана": "1148517845",   # 21.06.26_Praha_leads_Diana_model
    "Саида": "1993887386",   # fb.
    "Алиса": "448806878",    # fb2
    "Таня":  "651975206",    # fb3
    "Даша":  "871924069",    # fb4
    "Мага":  "978277453",    # fb5
}
# у частини рядків Meta не віддала назву оголошення через права доступу
BROKEN_AD = "не хватает разрешений"

def period_metrics(periods):
    """periods = {key:(dfrom,dto)} -> {manager:{key:{ctr,cpm,freq,...}}}
    Запит БЕЗ розбивки по днях — інакше frequency порахується неправильно."""
    out = {}
    for key, (dfrom, dto) in periods.items():
        try:
            rows = meta_rows(["account_id", "campaign", "spend", "impressions", "clicks", "reach"],
                             dfrom, dto)
        except Exception as e:
            print("  meta period FAIL", key, "->", e); continue
        agg = {}
        for r in rows:
            if not acct_ok(r): continue
            kind, m = classify(r.get("campaign"))
            if m is None: continue          # inst-картки теж отримують CTR/CPM/частоту
            a = agg.setdefault(m, {"imp": 0.0, "clicks": 0.0, "spend": 0.0, "reach": 0.0})
            a["imp"]    += num(r.get("impressions")); a["clicks"] += num(r.get("clicks"))
            a["spend"]  += num(r.get("spend"));       a["reach"]  += num(r.get("reach"))
        for m, a in agg.items():
            out.setdefault(m, {})[key] = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
    return out

def fetch_raw_map(barca_camps=frozenset()):
    """phone9 -> {m, ad_id, adset_id, ad, adset} із сирих вкладок.

    ВАЖЛИВО: зв'язуємо по ID, а не по НАЗВІ. У сирій вкладці лежить назва ад-сета
    на момент захоплення ліда; Ірина ад-сети перейменовує, тому join по назві
    втрачає ліди (перевірено: tochka15 — 62 ліди в Meta, 18 при join по назві)."""
    raw_map, raw_ok = {}, []
    for mgr, gid in RAW_GID.items():
        try:
            n = 0
            for r in fetch_sheet_rows(gid):
                ph     = phone9(r.get("phone_number"))
                ad_id  = _strip_id_prefix(r.get("ad_id"))
                as_id  = _strip_id_prefix(r.get("adset_id"))
                if not ph or not ad_id:
                    continue
                if BROKEN_AD in ad_id or BROKEN_AD in as_id:
                    continue                     # Meta не віддала ID через права доступу
                raw_map[ph] = {"m": mgr, "ad_id": ad_id, "adset_id": as_id,
                               "ad":    str(r.get("ad_name") or ""),
                               "adset": str(r.get("adset_name") or "")}
                n += 1
            if n:
                raw_ok.append(mgr)
            print("  сира вкладка %-6s -> %d лідів з ID" % (mgr, n))
        except Exception as e:
            print("  сира вкладка НЕ прочиталась:", mgr, "->", str(e)[:90])
    # Барселона (окрема таблиця, прив'язка тільки по кампаніях Ірини)
    try:
        n = 0
        for ph, hit in fetch_barcelona_raw(barca_camps).items():
            if hit["ad_id"]:
                raw_map[ph] = hit; n += 1
        if n:
            raw_ok.append(BARCA_MGR)
        print("  сира вкладка %-6s -> %d лідів з ID" % (BARCA_MGR, n))
    except Exception as e:
        print("  сира вкладка НЕ прочиталась:", BARCA_MGR, "->", str(e)[:90])
    print("  сирі вкладки працюють для:", ", ".join(raw_ok) if raw_ok else "нікого")
    return raw_map, raw_ok

def _in_period(cd, dfrom, dto):
    """cd/dfrom/dto — ISO-рядки дат; для ISO порівняння рядків == порівнянню дат."""
    return bool(cd) and dfrom <= cd <= dto

def _active_ids(id_field, yest_s):
    """ID (ад-сетів чи оголошень) з витратами > 0 вчора/сьогодні — «активні».
    Один і той самий набір для всіх періодів, щоб таблиці не розросталися
    від давно вимкнених."""
    recent = meta_rows(["account_id", id_field, "spend"], yest_s, TODAY)
    return {str(r.get(id_field) or "") for r in recent
            if acct_ok(r) and num(r.get("spend")) > 0}

def build_adsets(periods, yest_s, bk, raw_map, raw_ok):
    """Ад-сети ПО КОЖНОМУ ПЕРІОДУ: метрики Meta + ЯКІСТЬ лідів (нецільові / без
    реакції / записи). Повертає {period_key: [рядки]}. Період, який не вдалось
    отримати, просто відсутній у результаті — main() лишить для нього старі дані.
    Саме тут видно проблеми таргетингу (гео, радіус), яких не видно на рівні креативу."""
    active = _active_ids("adset_id", yest_s)
    out = {}
    for key, (dfrom, dto) in periods.items():
        try:
            rows = meta_rows(["account_id", "campaign", "adset_id", "adset_name", "spend",
                              "impressions", "clicks", "reach", "actions_lead"], dfrom, dto)
        except Exception as e:
            print("  ад-сети period FAIL", key, "->", e); continue
        agg = {}
        for r in rows:
            if not acct_ok(r): continue
            kind, m = classify(r.get("campaign"))
            if m is None: continue          # inst-ад-сети показуємо (витрати/CTR з Meta)
            aid = str(r.get("adset_id") or "")
            if not aid or aid not in active: continue
            a = agg.setdefault(aid, {"m": m, "campaign": r.get("campaign"),
                                     "adset": r.get("adset_name"),
                                     "spend": 0.0, "leads": 0,
                                     "imp": 0.0, "clicks": 0.0, "reach": 0.0})
            if r.get("adset_name"): a["adset"] = r.get("adset_name")   # завжди свіжа назва
            a["spend"] += num(r.get("spend")); a["leads"] += int(num(r.get("actions_lead")))
            a["imp"]   += num(r.get("impressions")); a["clicks"] += num(r.get("clicks"))
            a["reach"] += num(r.get("reach"))

        # якість лідів по ад-сетах: ліди періоду -> ад-сет ПО adset_id (не по назві!)
        qmap = {}   # adset_id -> {leads,bad,noresp,book}
        for mgr in (raw_ok if bk else []):
            for L in (bk.get(mgr, {}) or {}).get("leadsQ", []):
                if not _in_period(L["cd"], dfrom, dto):
                    continue
                hit = raw_map.get(L["ph"])
                if not hit or hit["m"] != mgr or not hit["adset_id"]:
                    continue
                q = qmap.setdefault(hit["adset_id"], {"leads": 0, "bad": 0, "noresp": 0, "book": 0})
                q["leads"] += 1
                if L["bad"]: q["bad"] += 1
                if L["nr"]:  q["noresp"] += 1
                if L["bk"]:  q["book"] += 1

        res = []
        for aid, a in agg.items():
            m = a["m"]; sp = round(a["spend"], 2); ld = a["leads"]
            rm = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
            q = qmap.get(aid) if m in raw_ok else None
            book = q["book"] if q else None
            res.append({"m": m, "campaign": a["campaign"], "adset": a["adset"],
                        "spend": sp, "leads": ld,
                        "cpl": round(sp / ld, 2) if ld else None,
                        "ctr": rm["ctr"], "freq": rm["freq"], "cpm": rm["cpm"],
                        "book": book,
                        "cpa": round(sp / book, 2) if book else None,
                        "q": q})
        res.sort(key=lambda x: -(x["spend"] or 0))
        out[key] = res
        print("  ад-сети [%-5s]: %d активних, якість є для %d"
              % (key, len(res), sum(1 for x in res if x["q"])))
    return out

def build_creatives(periods, yest_s, bk, raw_map, raw_ok):
    """Креативи ПО КОЖНОМУ ПЕРІОДУ. Показуємо ВСІ, що мали витрати в періоді;
    прапорець "act" = чи крутиться зараз (витрати вчора/сьогодні) — сайт
    показує неактивні сірим. Повертає {period_key: [рядки]}."""
    active = _active_ids("ad_id", yest_s)
    out = {}
    for key, (dfrom, dto) in periods.items():
        try:
            ads = meta_rows(["account_id", "campaign", "adset_name", "ad_id", "ad_name", "spend",
                             "impressions", "clicks", "reach", "actions_lead"], dfrom, dto)
        except Exception as e:
            print("  creatives period FAIL", key, "->", e); continue
        agg = {}
        for r in ads:
            if not acct_ok(r): continue
            kind, m = classify(r.get("campaign"))
            if m is None: continue          # inst-креативи показуємо (витрати/CTR з Meta)
            aid = str(r.get("ad_id") or "")
            if not aid: continue
            a = agg.setdefault(aid, {"m": m, "campaign": r.get("campaign"),
                                     "adset": r.get("adset_name"), "ad": r.get("ad_name"),
                                     "spend": 0.0, "leads": 0,
                                     "imp": 0.0, "clicks": 0.0, "reach": 0.0})
            if r.get("ad_name"):    a["ad"]    = r.get("ad_name")      # завжди свіжі назви
            if r.get("adset_name"): a["adset"] = r.get("adset_name")
            a["spend"] += num(r.get("spend")); a["leads"] += int(num(r.get("actions_lead")))
            a["imp"]   += num(r.get("impressions")); a["clicks"] += num(r.get("clicks"))
            a["reach"] += num(r.get("reach"))

        # записи періоду -> креатив ПО ad_id (не по назві!)
        bmap = {}   # ad_id -> book count
        for mgr in (raw_ok if bk else []):
            for L in (bk.get(mgr, {}) or {}).get("leadsQ", []):
                if not L["bk"] or not _in_period(L["cd"], dfrom, dto):
                    continue
                hit = raw_map.get(L["ph"])
                if hit and hit["m"] == mgr and hit["ad_id"]:
                    bmap[hit["ad_id"]] = bmap.get(hit["ad_id"], 0) + 1

        res = []
        for aid, a in agg.items():
            m = a["m"]; sp = round(a["spend"], 2); ld = a["leads"]
            rm = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
            book = bmap.get(aid, 0) if (m in raw_ok and bk and m in bk) else None
            res.append({"m": m, "name": a["ad"], "adset": a["adset"], "campaign": a["campaign"],
                        "act": aid in active,
                        "spend": sp, "leads": ld,
                        "cpl":  round(sp / ld, 2) if ld else None,
                        "book": book,
                        "cpa":  round(sp / book, 2) if book else None,
                        "conv": round(book / ld * 100, 1) if (book is not None and ld) else None,
                        "ctr": rm["ctr"], "freq": rm["freq"], "cpm": rm["cpm"]})
        res.sort(key=lambda c: (not c["act"], c["cpa"] if c["cpa"] is not None else 1e9))
        out[key] = res
        print("  creatives [%-5s]: %d рядків (%d активних)"
              % (key, len(res), sum(1 for c in res if c["act"])))
    return out

def _graph_all(edge, fields, extra=None):
    """GET /act_{ACCOUNT}/{edge} з пагінацією -> список рядків."""
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

def _local_date(ts):
    """created_time ад-сетів приходить з коректним зсувом — просто переводимо в TZ."""
    s = str(ts or "")
    try:
        if len(s) >= 5 and s[-5] in "+-" and ":" not in s[-5:]:
            s = s[:-2] + ":" + s[-2:]          # '+0000' -> '+00:00' для fromisoformat
        return datetime.datetime.fromisoformat(s).astimezone(TZ).date().isoformat()
    except Exception:
        return s[:10]

_ACT_TZ = None   # часовий пояс рекламного кабінету (лениво з API)

def _account_tz():
    """Часовий пояс РЕКЛАМНОГО КАБІНЕТУ — саме в ньому Ads Manager показує журнал
    змін, тому і ми показуємо час у ньому, щоб збігалося з тим, що бачить Ірина."""
    global _ACT_TZ
    if _ACT_TZ is not None:
        return _ACT_TZ
    tzname = off = None
    try:
        url = "%s/act_%s?%s" % (GRAPH_API, ACCOUNT, urllib.parse.urlencode(
            {"fields": "timezone_name,timezone_offset_hours_utc",
             "access_token": META_TOKEN}))
        info = http_json(url)
        tzname = info.get("timezone_name")
        off = info.get("timezone_offset_hours_utc")
        print("  account tz:", tzname, "| offset:", off)
    except Exception as e:
        print("  account tz FAIL:", str(e)[:100])
    try:
        from zoneinfo import ZoneInfo
        _ACT_TZ = ZoneInfo(tzname)
    except Exception:
        _ACT_TZ = (datetime.timezone(datetime.timedelta(hours=float(off)))
                   if off is not None else TZ)
    return _ACT_TZ

def _activity_local_dt(ts):
    """event_time з /activities — справжній UTC. Кабінет показує журнал у часовому
    поясі рекламного акаунта (НЕ празькому!) — переводимо туди ж, щоб дата й час на
    дашборді збігалися з журналом Ads Manager. (Зміна Діани «17.07 01:11» у кабінеті
    прийшла як 2026-07-16T21:11+0000 — кабінет виявився Asia/Dubai, UTC+4.)
    Повертає datetime або None, якщо не розпарсилось."""
    s = str(ts or "")
    try:
        if len(s) >= 5 and s[-5] in "+-" and ":" not in s[-5:]:
            s = s[:-2] + ":" + s[-2:]
        return datetime.datetime.fromisoformat(s).astimezone(_account_tz())
    except Exception:
        return None

def _budget_amt(v):
    """Сума з extra_data: центи числом або вкладений обʼєкт. Реальний формат Meta:
    {"old_value":{"type":"payment_amount","currency":"USD","old_value":1000,...},
     "new_value":{...,"new_value":1200,...}} — сума лежить ще раз під тим самим ключем."""
    if isinstance(v, str) and v[:1] in "{[":
        try:
            v = json.loads(v)
        except Exception:
            pass
    if isinstance(v, dict):
        for k in ("daily_budget", "lifetime_budget", "budget",
                  "new_value", "old_value", "value", "amount"):
            if v.get(k) is not None:
                v = v[k]
                break
    return round(num(v) / 100.0, 2) if num(v) > 0 else None

def fetch_scaling():
    """Блок «Масштабування»: активні ад-сети з денним бюджетом (свій або CBO-кампанії)
    + остання зміна бюджету з журналу кабінету (/activities). Рекомендацію рахує сайт.
    Без META_TOKEN повертає None -> старий блок у data.json лишається як був."""
    if not META_TOKEN:
        return None
    adsets = _graph_all("adsets",
                        "id,name,daily_budget,effective_status,created_time,"
                        "campaign{id,name,daily_budget}")
    # журнал змін бюджету: object_id -> остання подія (бюджети Meta віддає в центах)
    events = {}
    dbg = 0
    try:
        for ev in _graph_all("activities", "event_time,event_type,object_id,extra_data",
                             {"since": DATE_FROM, "limit": "500"}):
            et = str(ev.get("event_type") or "").lower()
            if "budget" not in et:
                continue
            if dbg < 8:   # діагностика в лог Actions: сирий формат подій
                print("  budget ev:", ev.get("event_time"), "|", et, "|",
                      str(ev.get("extra_data"))[:160])
                dbg += 1
            oid = str(ev.get("object_id") or "")
            t = str(ev.get("event_time") or "")
            if not oid or (oid in events and t <= events[oid]["t"]):
                continue
            old = new = None
            try:
                x = ev.get("extra_data") or {}
                if isinstance(x, str):
                    x = json.loads(x or "{}")
                # нульові/порожні суми Meta пише для частини подій — показуємо тільки реальні
                old = _budget_amt(x.get("old_value"))
                new = _budget_amt(x.get("new_value"))
            except Exception:
                pass
            events[oid] = {"t": t, "old": old, "new": new}
    except Exception as e:
        print("  activities FAIL (історія бюджетів недоступна):", str(e)[:120])
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
            ldt = _activity_local_dt(ev["t"])
            chg = {"date": ldt.date().isoformat() if ldt else str(ev["t"])[:10],
                   "time": ldt.strftime("%H:%M") if ldt else None,
                   "from": ev["old"], "to": ev["new"]}
        out.append({"m": m, "adset": a.get("name"), "campaign": camp.get("name"),
                    "budget": round(num(b) / 100.0, 2) if num(b) else None,
                    "lvl": lvl, "created": _local_date(a.get("created_time")),
                    "chg": chg})
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

    # нові менеджери: створюємо вузол при першому запуску; місто завжди синхронізуємо
    # з MANAGER_BOOTSTRAP (виправлення міста в коді підтягується і для існуючих вузлів)
    for m, meta in MANAGER_BOOTSTRAP.items():
        if m not in cur["managers"]:
            cur["managers"][m] = {"city": meta["city"], "start": meta["start"]}
            print("новий менеджер у data.json:", m)
        elif cur["managers"][m].get("city") != meta["city"]:
            print("місто виправлено:", m, cur["managers"][m].get("city"), "->", meta["city"])
            cur["managers"][m]["city"] = meta["city"]

    # кампанії Юлианы з кабінету Ірини — додатковий фільтр для сирих вкладок Барселони
    barca_camps = {str(r.get("campaign") or "") for r in rows
                   if classify(r.get("campaign")) == ("lead", BARCA_MGR)}
    if barca_camps:
        print("кампанії Барселони в кабінеті:", ", ".join(sorted(barca_camps)))

    lead_spend = {m: {} for m in cur["managers"]}
    lead_leads = {m: {} for m in cur["managers"]}
    for r in rows:
        kind, m = classify(r.get("campaign"))
        if m is None or m not in cur["managers"]:
            continue
        d = str(r.get("date"))[:10]
        sp = round(num(r.get("spend")), 4); ld = int(num(r.get("actions_lead")))
        lead_spend[m][d] = lead_spend[m].get(d, 0.0) + sp
        lead_leads[m][d] = lead_leads[m].get(d, 0) + ld

    # ліди Instagram Direct — вручну від Ірини (Meta їх не бачить як lead-екшн)
    for m, days in INST_MANUAL.items():
        if m in lead_leads:
            for d, v in days.items():
                lead_leads[m][d] = lead_leads[m].get(d, 0) + int(v.get("l", 0))

    out = json.loads(json.dumps(cur))
    out["updated"] = datetime.datetime.now(TZ).replace(microsecond=0).isoformat()
    out["metaSource"] = META_SOURCE["used"]   # graph | windsor — видно в шапці сайту
    out["periodNote"] = "оновлюється кожні 3 год з 7:00 до 22:00 за Прагою (вночі пауза)"

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
        node.pop("instSpend", None)   # інста тепер окремі картки, а не рядок у Діани/Аліси
        node["check"] = check_usd_node(m)   # середній чек для ROAS (None = не показуємо)
        if m in (INST_MGR_PARIS, INST_MGR_PRAGUE):
            # інста-картка «вимкнена», якщо нема витрат вчора/сьогодні — сайт сірить і ставить у кінець
            node["act"] = bool(sum(spend[-2:]) > 0)

    # Bookings from the sheet (defensive: keep carried values on any problem)
    try:
        bk = compute_bookings(barca_camps)
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

        # Реєстр «коли позначка запису вперше з'явилась» (_bookReg, сайт його ігнорує).
        # «Записи сьогодні» = записи з першою появою СЬОГОДНІ за місцевим часом (TZ) —
        # стійко до падінь/перезапусків, на відміну від старого порівняння зі знімком дня.
        # Міграція: при першій появі реєстру існуючі записи датуються "" і не рахуються.
        reg = cur.get("_bookReg") or {}
        today_local = datetime.datetime.now(TZ).date().isoformat()
        new_reg = dict(reg)   # менеджери, яких цей запуск не оновив, зберігають свій реєстр
        for m, v in bk.items():
            if m not in out["managers"] or v["leads_seen"] <= 0:
                continue
            booked_set = set(v["booked_keys"])
            mreg = dict(reg.get(m) or {})
            migrate = m not in reg
            for kk in booked_set:
                if kk not in mreg:
                    mreg[kk] = "" if migrate else today_local
            mreg = {kk: d for kk, d in mreg.items() if kk in booked_set}   # знятий запис -> геть
            new_reg[m] = mreg
            out["managers"][m]["bookingsToday"] = sum(1 for d in mreg.values() if d == today_local)
        out["_bookReg"] = new_reg

    # Записи Instagram-карток — з INST_MANUAL (незалежно від Google-таблиці)
    _td = datetime.date.fromisoformat(TODAY)
    for m, days in INST_MANUAL.items():
        node = out["managers"].get(m)
        if node is None:
            continue
        tot = b7 = bY = bM = b1 = 0
        for ds, v in days.items():
            b = int(v.get("b", 0))
            if not b:
                continue
            cd = datetime.date.fromisoformat(ds)
            tot += b
            if 0 <= (_td - cd).days < 7: b7 += b
            if cd == _td - datetime.timedelta(days=1): bY += b
            if cd >= _td.replace(day=1): bM += b
            if cd == _td: b1 += b
        node["bookings"] = tot; node["bookings7d"] = b7; node["bookingsYest"] = bY
        node["bookingsMonth"] = bM; node["bookings1d"] = b1; node["bookingsToday"] = b1

    # --- Meta-метрики по періодах (CTR / CPM / частота) + ад-сети й креативи по періодах ---
    td = datetime.date.fromisoformat(TODAY)
    yest_s = (td - datetime.timedelta(days=1)).isoformat()
    d7from = (td - datetime.timedelta(days=6)).isoformat()
    mstart = td.replace(day=1).isoformat()
    PERIODS = {"yest": (yest_s, yest_s), "d7": (d7from, TODAY),
               "month": (mstart, TODAY), "all": (DATE_FROM, TODAY)}
    try:
        pm = period_metrics(PERIODS)
        for m, node in out["managers"].items():
            if m in pm:
                node["m"] = pm[m]
    except Exception as e:
        print("period metrics failed -> skipping:", e)

    try:
        raw_map, raw_ok = fetch_raw_map(barca_camps)
    except Exception as e:
        print("raw map failed:", e); raw_map, raw_ok = {}, []

    def _merge_periods(field, fresh):
        """Свіжі періоди поверх старих: період, що не отримався, лишається зі
        старими даними (той самий принцип «ніколи не зануляти на помилці»)."""
        if not fresh:
            print(field, "failed entirely -> carrying over old ones"); return
        prev = out.get(field)
        if isinstance(prev, dict):        # старий формат (list) просто замінюємо
            merged = dict(prev); merged.update(fresh); fresh = merged
        out[field] = fresh

    try:
        _merge_periods("creatives", build_creatives(PERIODS, yest_s, bk, raw_map, raw_ok))
        out["creativesPeriod"] = "по періодах"
    except Exception as e:
        print("creatives failed -> carrying over old ones:", e)

    try:
        _merge_periods("adsets", build_adsets(PERIODS, yest_s, bk, raw_map, raw_ok))
    except Exception as e:
        print("adsets failed:", e)

    # бюджети + історія їх змін для блоку «Масштабування» (падає м'яко, старе лишається)
    try:
        sc = fetch_scaling()
        if sc is not None:
            out["scaling"] = sc
            print("scaling: %d активних ад-сетів, історія змін є для %d"
                  % (len(sc), sum(1 for x in sc if x["chg"])))
    except Exception as e:
        print("scaling failed -> carrying over:", str(e)[:150])

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
        print("%-12s spend %-8s leads %-4s book %-3s cpa %s"
              % (m, node["totalSpend"], node["totalLeads"], node.get("bookings"), node["cpa"]))

if __name__ == "__main__":
    main()

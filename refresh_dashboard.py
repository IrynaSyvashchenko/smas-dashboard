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
import os, re, json, time, datetime, urllib.request, urllib.parse, urllib.error, sys, html

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
TODAY     = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2))).date().isoformat()  # празька дата, щоб «Сьогодні» збігалося з «Оновлено» навіть уночі
DATA_FILE = "data.json"
TZ        = datetime.timezone(datetime.timedelta(hours=2))

LEAD_KW = {"Диана": "Prague_Diana", "Таня": "Tanya", "Алиса": "Alissa",
           "Саида": "Saida", "Даша": "Dasha",
           # Вика (Прага, з 12.08): РК «12_08_Prague_new» -> 17.08 перейменована в
           # «12_08_Prague_Vika». Meta віддає ІСТОРІЮ під ПОТОЧНОЮ назвою, тому тримаємо
           # обидві + ім'я — інакше при перейменуванні картка обнуляється.
           "Вика": ("Prague_new", "Prague_Vika", "Viktoria", "Vika"),
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
    INST_MGR_PRAGUE: {"2026-07-01": {"l": 36, "b": 18}},   # факт адміністратора 03.08: липень 36 лідів / 18 записів
    INST_MGR_PARIS:  {"2026-07-01": {"l": 0, "b": 14}},    # факт адміністратора 03.08: 14 записів за липень (ліди авто з Meta)
}

# Фактичні записи від АДМІНІСТРАТОРА салону по місяцях (він рахує всі записи менеджера).
# Сайт показує їх окремою вкладкою «<Місяць> (факт)». Ключ = "YYYY-MM".
# Ручні числа мають ПРІОРИТЕТ над авто-фактом з реєстру (липень звірений з адміном 03.08).
FACT_BOOKINGS = {
    "2026-07": {"Диана": 151, "Даша": 56, "Таня": 114, "Саида": 86, "Мага": 20,
                "Алиса": 76, "Юлиана": 15, "Инста Прага": 18, "Инста Париж": 14},
}

# Реєстр адміністратора: щоденна таблиця «дата | таргетолог | админ | название инст |
# новые заявки | запись из новых | запись из старых | в запись всего | ...».
ADMIN_SS  = "1-KjfT3Q8kc3UEijfynV1Ha5PGlXeLUL_Iu9IWS4iyLA"
ADMIN_GID = "1593394532"

def _admin_mgr(name):
    """«название инст» з реєстру -> менеджер дашборда. None = не наш/нерозпізнаний."""
    n = str(name or "").lower()
    if not n:
        return None
    if n.startswith("@"):
        return INST_MGR_PRAGUE if ("prague" in n or "praga" in n) else INST_MGR_PARIS
    if "диан" in n:                   return "Диана"     # «Лиды Прага Дианы» — ДО перевірки «прага»
    if "саида" in n:                  return "Саида"
    if "таня" in n:                   return "Таня"
    if "алиса" in n:                  return "Алиса"
    if "максим" in n or "мага" in n:  return "Мага"      # кампанія «Лиды Париж Максим» веде Мага
    if "яна" in n:                    return None        # Яна — менеджер іншого таргетолога
    if "юлиана" in n or "барселона" in n: return "Юлиана"
    if "вика" in n or "вікa" in n or "vika" in n: return "Вика"   # «Лиды Прага Вика» — НЕ Даша (грабля: серпень рахувався Даші)
    if "прага" in n:                  return "Даша"      # «Лиды Прага» без імені = Даша
    return None

def fetch_admin_fact():
    """Рядки таргетолога Ірини з реєстру адміністратора ->
    {"monthly": {"YYYY-MM": {mgr: записів}}, "daily": {mgr: {"YYYY-MM-DD": записів}},
     "unmapped": {назва: записів}} (нерозпізнані кампанії — щоб помітити нову)."""
    rows = fetch_sheet_rows(gid=ADMIN_GID, ss=ADMIN_SS)
    year = datetime.date.fromisoformat(TODAY).year   # дати в реєстрі без року (dd.mm)
    monthly, daily, unmapped = {}, {}, {}
    for r in rows:
        if "ирин" not in str(r.get("таргетолог") or "").lower():
            continue
        m = re.match(r"(\d{1,2})\.(\d{1,2})$", str(r.get("дата") or "").strip())
        if not m:
            continue
        try:
            dt = datetime.date(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            continue
        tot = int(num(r.get("в_запись_всего")))
        mgr = _admin_mgr(r.get("название_инст"))
        if mgr is None:
            if tot:
                k = str(r.get("название_инст") or "?")
                unmapped[k] = unmapped.get(k, 0) + tot
            continue
        mk, ds = dt.isoformat()[:7], dt.isoformat()
        monthly.setdefault(mk, {})[mgr] = monthly.get(mk, {}).get(mgr, 0) + tot
        daily.setdefault(mgr, {})[ds] = daily.get(mgr, {}).get(ds, 0) + tot
    return {"monthly": monthly, "daily": daily, "unmapped": unmapped}

# Нові менеджери: вузол у data.json створюється автоматично при першому запуску
MANAGER_BOOTSTRAP = {"Юлиана": {"city": "Барселона", "start": "2026-07-09"},
                     "Мага":   {"city": "Париж", "start": "2026-07-18"},
                     "Вика":   {"city": "Прага", "start": "2026-08-12"},
                     INST_MGR_PARIS:  {"city": "Париж", "start": "2026-06-20"},
                     INST_MGR_PRAGUE: {"city": "Прага", "start": "2026-06-20"}}

# Вимкнені менеджери (Ірина, 26.08): картка сіра і в кінці, дані лишаються.
MANAGER_OFF  = ("Вика",)
# Приховані зовсім (тимчасово, теж 26.08): сайт і бриф їх не показують,
# дані в data.json продовжують збиратись — розховати = прибрати звідси.
MANAGER_HIDE = ("Юлиана", INST_MGR_PARIS, INST_MGR_PRAGUE)

# --- ROAS: середній чек (записи x чек / витрати; оцінка, бо запис != оплачена процедура) ---
# Курси до USD (валюта кабінету) — константи, онови за потреби.
EUR_USD = 1.17
CZK_USD = 0.047
AVG_CHECK = {   # (сума, валюта). Париж/Барселона — ціна оферу в кампанії; Прага — «модельна» ціна.
    # Диана: з 18.07 акція для моделей 2999 Kč (стара модельна 3990 лишилась у Даші/інсти).
    "Диана": (2999, "CZK"), "Даша": (3990, "CZK"),
    "Вика":  (2999, "CZK"),   # Прага, форма «...Viktoria_model» = модельна акція
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
# Значення: gid у головній таблиці АБО (ss, gid) — якщо менеджер в окремому файлі.
VIKA_SS = "1Tpxch9P2Jadj_33cLLxCaRqfamW8W_TUWfw7MJdmrpk"   # «Прага Вика» (з 12.08)
MANAGER_GID = {"Диана": "1178192251", "Таня": "1053387771",
               "Алиса": "36427361", "Саида": "2065248461", "Даша": "1406387900",
               "Мага": "1445117368",
               "Вика": (VIKA_SS, "994125991")}

# Додаткові CRM-вкладки менеджера (за НАЗВОЮ): нова РК -> нова вкладка ліда.
# «Алиса Ирина модель» — ліди 149-ї (модельної) РК Алиси (Ірина, 26.08).
# Рядки просто доливаються до основної вкладки; дедуп по (телефон, created_time).
MANAGER_EXTRA_SHEETS = {"Алиса": ("Алиса Ирина модель",)}

def _sheet_args(v):
    """gid або (ss, gid) -> kwargs для fetch_sheet_rows."""
    return {"ss": v[0], "gid": v[1]} if isinstance(v, (tuple, list)) else {"gid": v}
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
    reach/frequency коректні (грабля: частоту не можна сумувати по днях).

    Денний зріз ріжеться на вікна: 24.08.2026 запит за 66 днів з time_increment=1
    почав стабільно вертати HTTP 500 code 1 / subcode 99 («забагато даних»), скрипт
    відкочувався на Windsor, а там від'єднаний FB-акаунт -> усі витрати ставали 0."""
    if "date" in fields:
        return _graph_daily_chunked(fields, dfrom, dto)
    return _graph_once(fields, dfrom, dto)

def _graph_daily_chunked(fields, dfrom, dto, span=14):
    """Денний зріз вікнами по span днів; вікно, що впало на «забагато даних»,
    ріжеться навпіл (до одного дня). Так довжина історії більше не впирається в ліміт."""
    d1 = datetime.date.fromisoformat(dfrom); d2 = datetime.date.fromisoformat(dto)
    out, win = [], []
    cur = d1
    while cur <= d2:
        end = min(cur + datetime.timedelta(days=span - 1), d2)
        win.append((cur, end)); cur = end + datetime.timedelta(days=1)
    for a, b in win:
        out += _graph_split(fields, a, b)
    return out

def _graph_split(fields, a, b):
    """Одне вікно; при «забагато даних» ділимо навпіл, поки не пройде."""
    try:
        return _graph_once(fields, a.isoformat(), b.isoformat())
    except RuntimeError as e:
        if "HTTP 500" not in str(e) or a == b:
            raise
        mid = a + (b - a) // 2
        print("  graph: вікно %s..%s завелике, ділю навпіл" % (a, b))
        return _graph_split(fields, a, mid) + _graph_split(fields, mid + datetime.timedelta(days=1), b)

def _graph_once(fields, dfrom, dto):
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
def _book_date_cell(r):
    """Значення колонки дати запису. Назва в різних таблицях різна:
    «дата_и_время_записи», у Вики — «[merged]_дата_и_время_записи»."""
    for k, v in r.items():
        if "дата_и_время_записи" in str(k):
            s = str(v or "").strip()
            if s not in ("", "0", "-", "—", "None"):
                return s
    return ""

def is_booked(r):
    if _book_date_cell(r):
        return True
    for sf in STATUS_FIELDS:
        s = str(r.get(sf) or "").lower()
        if "отказ" in s:
            continue
        # «запис» ловить усі форми: «запись», «записана», «записалась» (і одруківки).
        # Заперечення й «уже запис…» (людину вже записав ІНШИЙ таргетолог — правило Ірини)
        # ВИРІЗАЄМО з тексту, а не відкидаємо всю клітинку: якщо ПІСЛЯ цього лишилось
        # «запис» — це вже НОВИЙ запис Ірини, і він рахується
        # (статуси в різних колонках і так перевіряються незалежно: «Уже записана»
        #  в одній колонці + «Запись» у наступній = рахується).
        for n in ("не запис", "без запис", "уже запис"):
            s = s.replace(n, "")
        if "запис" in s:
            return True
    return False

def _book_date(r):
    """Дата САМОГО ЗАПИСУ з «дата_и_время_записи» (для аудиту: адміністратор рахує
    записи по цій даті, а дашборд — по даті створення ліда). None = не розпарсилось."""
    s = _book_date_cell(r)
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)          # ISO 2026-07-29
    if m:
        try: return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError: return None
    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", s)   # 29.07 / 29.07.26 / 29/07/2026
    if m:
        y = m.group(3)
        y = int(y) if y else datetime.date.fromisoformat(TODAY).year
        if y < 100: y += 2000
        try: return datetime.date(y, int(m.group(2)), int(m.group(1)))
        except ValueError: return None
    return None

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
    # міст інколи флейкає (разові 5xx/timeout) — до 3 спроб, щоб один збій
    # не лишав менеджера без записів/якості на цілий цикл оновлення
    payload = None; last_err = None
    for _att in range(3):
        try:
            payload = http_json(url)       # Apps Script віддає 302 -> urllib йде за ним сам
            break
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
                # дублікати заголовків (у Вики три колонки «написал на WhatsApp»):
                # НЕ затираємо вже заповнене значення порожнім дублем
                if h in d and str(raw[i] or "").strip() == "":
                    continue
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
    pm_start = (month_start - datetime.timedelta(days=1)).replace(day=1)   # попередній місяць
    booked = b7 = b3 = b1 = leads = 0
    bY = bM = bPM = 0                   # bookings among leads created yesterday / this / prev month
    old_keys = []                       # keys of booked leads created BEFORE today
    booked_keys = []                    # ключі ВСІХ поточних записів (для реєстру «коли з'явився запис»)
    # lead quality per period: leads / нецільові / без реакції (ліквід = leads-bad-noresp)
    Q = {p: {"leads": 0, "bad": 0, "noresp": 0} for p in ("yest", "d3", "d7", "month", "pmonth", "all")}
    leadsQ = []   # усі мої ліди з телефоном: дата + прапорці (для таблиць ад-сетів/креативів по періодах)
    # АУДИТ підрахунку записів (порівняння з фактами адміністратора):
    #   pm_bd/m_bd  = записи по ДАТІ ЗАПИСУ (дата_и_время_записи) у попер./поточ. місяці
    #   bd_none     = записи, де дату запису не вдалось розпарсити
    #   miss        = НЕ пораховані рядки, але зі словом «запис» у статусах (варіанти
    #                 типу «записалась», яких «запись»-матчер не ловить) + приклади
    #   pm_others   = записи серед ЧУЖИХ лідів (інший таргетолог) з датою ліда в попер.
    #                 місяці — перевірка гіпотези «адміністратор рахує всіх, дашборд — Ірину»
    #   empty_targ      = рядки з ПОРОЖНЬОЮ колонкою «таргетолог» (їх рахуємо як Ірини —
    #                     див. _iryna_row); empty_targ_bk — скільки з них із записом.
    #                     Якщо адмін каже менше записів, ніж дашборд, дивись сюди:
    #                     ймовірно, це чужі ліди без підпису таргетолога.
    audit = {"pm_bd": 0, "m_bd": 0, "bd_none": 0, "miss": 0, "samples": [],
             "pm_others": 0, "others_bk": 0, "empty_targ": 0, "empty_targ_bk": 0,
             "empty_targ_bk_today": 0}

    for k, r in seen.items():
        if not row_ok(r):
            if is_booked(r):
                audit["others_bk"] += 1
                try:
                    cd0 = datetime.date.fromisoformat(str(r.get("created_time") or "")[:10])
                    if pm_start <= cd0 < month_start: audit["pm_others"] += 1
                except Exception:
                    pass
            continue
        # рядок без підпису таргетолога — рахується як наш; фіксуємо для звірки з адміном
        if not str(r.get("таргетолог") or "").strip():
            audit["empty_targ"] += 1
            if is_booked(r):
                audit["empty_targ_bk"] += 1
                if str(r.get("created_time") or "")[:10] == today.isoformat():
                    audit["empty_targ_bk_today"] += 1
        leads += 1
        ct = str(r.get("created_time") or "")[:10]
        try:
            cd = datetime.date.fromisoformat(ct)
        except Exception:
            cd = None

        bk = is_booked(r)
        bd = (not bk) and is_bad(r)
        nr = (not bk) and (not bd) and is_noresp(r)

        # -- аудит: записи по даті ЗАПИСУ + пропущені варіанти позначок --
        if bk:
            _bdt = _book_date(r)
            if _bdt is None:
                audit["bd_none"] += 1
            else:
                if pm_start <= _bdt < month_start: audit["pm_bd"] += 1
                if _bdt >= month_start:            audit["m_bd"] += 1
        else:
            _st = _statuses(r)
            if "запис" in _st:
                audit["miss"] += 1
                if len(audit["samples"]) < 12:
                    _v = next((str(r.get(sf)) for sf in STATUS_FIELDS
                               if "запис" in str(r.get(sf) or "").lower()), "")
                    if _v and _v not in audit["samples"]:
                        audit["samples"].append(_v[:60])

        buckets = ["all"]
        if cd:
            if cd == yest: buckets.append("yest")
            if 0 <= (today - cd).days < 3: buckets.append("d3")
            if 0 <= (today - cd).days < 7: buckets.append("d7")
            if cd >= month_start: buckets.append("month")
            if pm_start <= cd < month_start: buckets.append("pmonth")
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
                if 0 <= (today - cd).days < 3:
                    b3 += 1
                if cd == yest: bY += 1
                if cd >= month_start: bM += 1
                if pm_start <= cd < month_start: bPM += 1
                if cd == today:
                    b1 += 1
                elif cd < today:
                    old_keys.append("%s|%s" % (k[0], k[1]))
            else:
                old_keys.append("%s|%s" % (k[0], k[1]))   # unknown date -> treat as old

    return {"bookings": booked, "bookings7d": b7, "bookings3d": b3, "bookings1d": b1, "bkAudit": audit,
            "bookingsYest": bY, "bookingsMonth": bM, "bookingsPrevMonth": bPM,
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
            rows = fetch_sheet_rows(**_sheet_args(gid))
        except Exception as e:
            print("  sheet FAIL", mgr, gid, "->", e); continue
        # додаткові CRM-вкладки (нові РК): доливаємо рядки; збій дод. вкладки не
        # валить основну (дедуп у _tally_manager по телефону+created_time)
        for extra in MANAGER_EXTRA_SHEETS.get(mgr, ()):
            try:
                add = fetch_sheet_rows(sheet=extra)
                rows = list(rows) + list(add)
                print("  дод. CRM-вкладка «%s» (%s): %d рядків" % (extra, mgr, len(add)))
            except Exception as e:
                print("  дод. CRM-вкладка «%s» FAIL ->" % extra, str(e)[:80])
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
    "Вика":  (VIKA_SS, None),   # вкладка «Fb» в окремій таблиці (за назвою, не за gid)
}
# у частини рядків Meta не віддала назву оголошення через права доступу
BROKEN_AD = "не хватает разрешений"
RAW_STATS = {}   # {вкладка: {"rows": N, "last_lead": "YYYY-MM-DD"}} — чи жива сира вкладка
# Нові сирі вкладки (після зміни лід-форм експорт пише в НОВІ вкладки — 06.08 знайдено
# fb6..fb9, "fb1,"). Менеджер визначається ПО РЯДКУ через classify(campaign_name),
# тож майбутні нові вкладки досить дописати сюди за назвою.
RAW_EXTRA = ["fb6", "fb7", "fb8", "fb9", "fb1,",
             # 20.08: знайдено ще чотири живі вкладки, яких не було в списку — через це
             # у Алисы (fbb/afb) і Диани (dfb) записи не сідали на ад-сети тижнями.
             "fbb", "afb", "dfb", "fbV", "fb4",
             # кандидати «на виріст»: при зміні форми експорт створює наступну вкладку.
             # Неіснуючі просто не прочитаються (м'яка помилка) — див. _diag.rawTabs.
             "fb10", "fb11", "fb12", "fb13", "fb14", "fb15", "fb2,", "fb3,"]
# Вгадувати назви більше не треба: беремо список вкладок із самої таблиці. Пропускаємо
# копії/бекапи — вони містять СТАРІ рядки з тими самими телефонами і перетерли б свіжу
# прив'язку до ад-сета.
RAW_SKIP_WORDS = ("copy", "recover", "backup", "бекап", "копия", "копія")
# Автоберемо лише вкладки-ЕКСПОРТИ лід-форм: назва містить «fb» (fb2, fbb, afb, dfb,
# fbV…) або починається з дати форми («28.04.26_leads_…»). CRM-вкладки менеджерів
# («Саида Влад», «Прага Диана») читаються за gid і другий раз не потрібні.
RAW_TAB_RE = re.compile(r"(fb|^\d\d[.\-_ ]\d\d[.\-_ ]\d\d)", re.I)
SHEET_TABS = []   # діагностика: всі НЕприховані вкладки таблиці (_diag.sheetTabs)

def list_tabs(ss=None):
    """Назви всіх вкладок таблиці (сторінка htmlview, читається без токена).
    Будь-який збій -> [] і працюємо далі за статичним RAW_EXTRA."""
    ss = ss or SHEET_ID
    try:
        req = urllib.request.Request("https://docs.google.com/spreadsheets/d/%s/htmlview" % ss,
                                     headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
    except Exception as e:
        print("  список вкладок НЕ отримано ->", str(e)[:80]); return []
    # htmlview будує меню з items.push({name: "...", ..., gid: "..."}) — беремо звідти.
    # ВАЖЛИВО: приховані вкладки (fb6, fb2, fb3…) сюди не потрапляють, тому це
    # ДОПОВНЕННЯ до RAW_EXTRA, а не заміна.
    names = re.findall(r'\{name:\s*"([^"]{1,80})"[^}]*gid:\s*"\d+"', page)
    return [html.unescape(n).strip() for n in names if n.strip()]

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
    raw_map, got = {}, {}
    RAW_STATS.clear()   # діагностика: чи вкладка ЖИВА (наповнюється новими лідами)

    def _ingest(rows_all, default_mgr, tabname):
        """Рядки однієї вкладки -> raw_map. Менеджер рядка = classify(campaign_name)
        (форми міняються, і той самий tab може містити кампанії різних менеджерів);
        fallback — default_mgr вкладки (для рядків без розпізнаної кампанії)."""
        n = 0
        _lastc = max((str(r.get("created_time") or "")[:10] for r in rows_all), default="")
        RAW_STATS[tabname] = {"rows": len(rows_all), "last_lead": _lastc}
        for r in rows_all:
            ph     = phone9(r.get("phone_number"))
            ad_id  = _strip_id_prefix(r.get("ad_id"))
            as_id  = _strip_id_prefix(r.get("adset_id"))
            if BROKEN_AD in ad_id: ad_id = ""    # Meta не віддала ad_id через права
            if BROKEN_AD in as_id: as_id = ""
            # для АД-СЕТ-атрибуції досить adset_id — не викидаємо рядок лише через брак ad_id
            if not ph or (not ad_id and not as_id):
                continue
            _k, _m = classify(r.get("campaign_name"))
            mgr = _m or default_mgr
            if not mgr:
                continue
            raw_map[ph] = {"m": mgr, "ad_id": ad_id, "adset_id": as_id,
                           "ad":    str(r.get("ad_name") or ""),
                           "adset": str(r.get("adset_name") or "")}
            got[mgr] = got.get(mgr, 0) + 1
            n += 1
        print("  сира вкладка %-8s -> %d лідів з ID (останній %s)" % (tabname, n, _lastc or "?"))

    for mgr, gid in RAW_GID.items():
        try:
            args = {"ss": gid[0], "sheet": "Fb"} if isinstance(gid, (tuple, list)) else {"gid": gid}
            _ingest(fetch_sheet_rows(**args), mgr, mgr)
        except Exception as e:
            print("  сира вкладка НЕ прочиталась:", mgr, "->", str(e)[:90])
    # нові вкладки (експорт після зміни форм) — менеджер лише з campaign_name.
    # Список доповнюємо реальними вкладками таблиці, щоб чергова нова вкладка
    # підхопилась сама, а не через місяць, коли CPA поїде.
    extra = list(RAW_EXTRA)
    SHEET_TABS[:] = list_tabs()
    for t in SHEET_TABS:
        if t in extra or any(w in t.lower() for w in RAW_SKIP_WORDS):
            continue
        if RAW_TAB_RE.search(t):
            extra.append(t)
    for name in extra:
        try:
            _ingest(fetch_sheet_rows(sheet=name), None, name)
        except Exception as e:
            print("  дод. вкладка", name, "->", str(e)[:60])
    raw_ok = [m for m, c in got.items() if c > 0]
    # Барселона (окрема таблиця, прив'язка тільки по кампаніях Ірини)
    try:
        n = 0
        for ph, hit in fetch_barcelona_raw(barca_camps).items():
            if hit["ad_id"] or hit["adset_id"]:     # для ад-сета досить adset_id
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
    # liveA = ад-сети, що ЗАРАЗ реально крутяться (effective_status ACTIVE). Вимкнені
    # НЕ ховаємо — показуємо сірим (act=False), як креативи, щоб бачити виключене.
    liveA = ({str(x.get("id")) for x in _graph_all("adsets", "id,effective_status") if x.get("effective_status") == "ACTIVE"} if META_TOKEN else None)
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
            if not aid: continue
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
                # НЕ вимагаємо hit["m"]==mgr: у Парижі сирі вкладки перехресні, тому мітка
                # менеджера в raw_map ненадійна, а adset_id — правильний. Менеджера ад-сета
                # визначає кампанія (classify). Раніше ця вимога губила 11/13 записів Саиди.
                if not hit or not hit["adset_id"]:
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
                        "act": (aid in liveA) if liveA is not None else True,
                        "spend": sp, "leads": ld,
                        "cpl": round(sp / ld, 2) if ld else None,
                        "ctr": rm["ctr"], "freq": rm["freq"], "cpm": rm["cpm"],
                        "book": book,
                        "cpa": round(sp / book, 2) if book else None,
                        "q": q})
        res.sort(key=lambda x: (x.get("act") is False, -(x["spend"] or 0)))
        out[key] = res
        print("  ад-сети [%-5s]: %d рядків (%d активних), якість є для %d"
              % (key, len(res), sum(1 for x in res if x.get("act") is not False),
                 sum(1 for x in res if x["q"])))
    return out

def build_creatives(periods, yest_s, bk, raw_map, raw_ok):
    """Креативи ПО КОЖНОМУ ПЕРІОДУ. Показуємо ВСІ, що мали витрати в періоді;
    прапорець "act" = чи крутиться зараз (effective_status ACTIVE з кабінету) — сайт
    показує неактивні сірим. Повертає {period_key: [рядки]}."""
    active = _active_ids("ad_id", yest_s); live = ({str(x.get("id")) for x in _graph_all("ads", "id,effective_status") if x.get("effective_status") == "ACTIVE"} if META_TOKEN else None)
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
                if hit and hit["ad_id"]:      # мітка менеджера в raw_map ненадійна (див. build_adsets)
                    bmap[hit["ad_id"]] = bmap.get(hit["ad_id"], 0) + 1

        res = []
        for aid, a in agg.items():
            m = a["m"]; sp = round(a["spend"], 2); ld = a["leads"]
            rm = rate_metrics(a["imp"], a["clicks"], a["spend"], a["reach"])
            book = bmap.get(aid, 0) if (m in raw_ok and bk and m in bk) else None
            res.append({"m": m, "name": a["ad"], "adset": a["adset"], "campaign": a["campaign"],
                        "act": (aid in live) if live is not None else (aid in active),
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

HIST_FETCH_DAYS = 35   # скільки днів тягнемо з Meta при кожному оновленні
HIST_KEEP_DAYS  = 70   # скільки днів історії тримаємо в data.json

def build_adset_hist(cur, bk, raw_map, raw_ok):
    """Щоденна історія по ад-сетах: {adset_id: {m, adset, campaign, days:{дата:[spend, leads, book]}}}.

    Фундамент аналітики масштабування: тренд CPL по днях, порівняння «до/після»
    зміни бюджету, вік нової РК. Свіжі HIST_FETCH_DAYS днів перезаписуються при
    кожному оновленні (записи доганяють ліди з лагом), старіші дні зберігаються
    зі старого data.json до HIST_KEEP_DAYS. Повертає None, якщо Meta нічого не
    віддала — тоді стара історія лишається як була."""
    td = datetime.date.fromisoformat(TODAY)
    hfrom = max(DATE_FROM, (td - datetime.timedelta(days=HIST_FETCH_DAYS - 1)).isoformat())
    rows = meta_rows(["date", "account_id", "campaign", "adset_id", "adset_name",
                      "spend", "actions_lead"], hfrom, TODAY)
    fresh = {}
    for r in rows:
        if not acct_ok(r):
            continue
        kind, m = classify(r.get("campaign"))
        if m is None:
            continue
        aid = str(r.get("adset_id") or ""); d = str(r.get("date") or "")[:10]
        if not aid or not d:
            continue
        h = fresh.setdefault(aid, {"m": m, "adset": r.get("adset_name"),
                                   "campaign": r.get("campaign"), "days": {}})
        if r.get("adset_name"): h["adset"] = r.get("adset_name")
        cell = h["days"].setdefault(d, [0.0, 0, 0])
        cell[0] = round(cell[0] + num(r.get("spend")), 2)
        cell[1] += int(num(r.get("actions_lead")))
    if not fresh:
        return None
    # записи по днях (за датою СТВОРЕННЯ ліда) -> ад-сет по adset_id —
    # та сама атрибуція, що в build_adsets (по ID, не по назві)
    for mgr in (raw_ok if bk else []):
        for L in (bk.get(mgr, {}) or {}).get("leadsQ", []):
            if not L["bk"] or not L["cd"] or L["cd"] < hfrom:
                continue
            aid = (raw_map.get(L["ph"]) or {}).get("adset_id")
            if aid in fresh:
                fresh[aid]["days"].setdefault(L["cd"], [0.0, 0, 0])[2] += 1
    # злиття зі старою історією: свіжі дні поверх, старіші за вікно — лишаються
    cutoff = (td - datetime.timedelta(days=HIST_KEEP_DAYS)).isoformat()
    prev = cur.get("adsHist") or {}
    for aid, ph in prev.items():
        old_days = {d: v for d, v in (ph.get("days") or {}).items()
                    if cutoff <= d < hfrom}
        if aid in fresh:
            merged = dict(old_days); merged.update(fresh[aid]["days"])
            fresh[aid]["days"] = merged
        elif old_days:
            fresh[aid] = {"m": ph.get("m"), "adset": ph.get("adset"),
                          "campaign": ph.get("campaign"), "days": old_days}
    return fresh

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
    # журнал змін бюджету: остання подія (для chg) + ПОВНИЙ лог по кожному object_id
    # (для аналізу «CPL до/після зміни»; бюджети Meta віддає в центах)
    events = {}
    evlog = {}
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
            if not oid:
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
            evlog.setdefault(oid, []).append({"t": t, "old": old, "new": new})
            if oid not in events or t > events[oid]["t"]:
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
        out.append({"m": m, "id": str(a.get("id")), "adset": a.get("name"),
                    "campaign": camp.get("name"),
                    "budget": round(num(b) / 100.0, 2) if num(b) else None,
                    "lvl": lvl, "created": _local_date(a.get("created_time")),
                    "chg": chg})
    return out, evlog

def _dd(s):
    return "%s.%s" % (s[8:10], s[5:7]) if s and len(str(s)) >= 10 else str(s or "")

def _range_cpl(days, dfrom, dto):
    """days = {дата: [spend, leads, book]} -> (spend, leads, cpl) за діапазон дат."""
    sp = sum(v[0] for d, v in days.items() if dfrom <= d <= dto)
    ld = sum(v[1] for d, v in days.items() if dfrom <= d <= dto)
    return round(sp, 2), ld, (round(sp / ld, 2) if ld else None)

def build_lifecycle(out):
    """Життєвий цикл кожного активного ад-сета з бюджетом: вік, днів на поточному
    бюджеті, CPL за 3/7 днів, CPL ДО і ПІСЛЯ останньої зміни бюджету (з adsHist)
    і ВЕРДИКТ масштабування. Рахується один раз тут — сайт і Telegram-бриф лише
    показують, щоб рекомендації ніде не розходились."""
    sc = out.get("scaling"); hist = out.get("adsHist") or {}
    if not sc:
        return None
    td = datetime.date.fromisoformat(TODAY)
    d3s = (td - datetime.timedelta(days=2)).isoformat()
    d7s = (td - datetime.timedelta(days=6)).isoformat()
    # базова лінія для нових РК: CPL менеджера за останні 7 днів
    mcpl = {}
    for m, node in out["managers"].items():
        sp = ld = 0
        for i, d in enumerate(node.get("dates") or []):
            if d >= d7s:
                sp += (node["spend"][i] or 0); ld += (node["leads"][i] or 0)
        mcpl[m] = round(sp / ld, 2) if ld else None
    _key = lambda a: (a.get("m"), a.get("campaign"), a.get("adset"))
    d7map = {_key(a): a for a in (out.get("adsets", {}).get("d7") or [])}
    momap = {_key(a): a for a in (out.get("adsets", {}).get("month") or [])}
    # запобіжник атрибуції: якщо на ад-сети менеджера сіло <60% його записів за
    # 7 днів (сира вкладка відстає) — адсет-CPA завищена, вердикти по CPA не робимо
    as_bk7 = {}
    for a in (out.get("adsets", {}).get("d7") or []):
        as_bk7[a["m"]] = as_bk7.get(a["m"], 0) + (a.get("book") or 0)
    def _unrel(m):
        c = out["managers"].get(m, {}).get("bookings7d") or 0
        return c >= 5 and as_bk7.get(m, 0) < c * 0.6
    res = []
    for s in sc:
        m = s["m"]; aid = str(s.get("id") or "")
        h = (hist.get(aid) or {}).get("days") or {}
        a7 = d7map.get(_key(s)); amo = momap.get(_key(s))
        chk = (check_usd_node(m) or {}).get("usd")
        created = s.get("created")
        chg = s.get("chg") or None
        chg_date = (chg or {}).get("date")
        if chg_date and created and chg_date < created:
            chg_date = None                 # подія старша за ад-сет — не його
        since = chg_date or created
        age = ((td - datetime.date.fromisoformat(created)).days + 1) if created else None
        days_at = (td - datetime.date.fromisoformat(since)).days if since else None
        sp3, ld3, cpl3 = _range_cpl(h, d3s, TODAY)
        sp7h, ld7, cpl7 = _range_cpl(h, d7s, TODAY)
        cplB = cplA = None; ldB = ldA = 0
        if chg_date:
            _cd = datetime.date.fromisoformat(chg_date)
            _, ldB, cplB = _range_cpl(h, (_cd - datetime.timedelta(days=7)).isoformat(),
                                      (_cd - datetime.timedelta(days=1)).isoformat())
            _, ldA, cplA = _range_cpl(h, chg_date, TODAY)
        unrel = _unrel(m)
        freq = (a7 or {}).get("freq")
        book7 = None if unrel else (a7 or {}).get("book")
        cpa7 = None if unrel else (a7 or {}).get("cpa")
        cpaM = None if unrel else (amo or {}).get("cpa")
        cplM = (amo or {}).get("cpl")
        spend7 = (a7 or {}).get("spend") if a7 else sp7h
        budget = s.get("budget") or 0
        base = mcpl.get(m)
        up = ("$%.0f -> $%.2f–$%.2f/день" % (budget, budget * 1.25, budget * 1.3)) if budget else "+20–30%"
        # ---- вердикт: смерть -> деградація -> нова -> черга/частота -> дорого -> масштабуй
        if str(m).startswith("Инста"):
            v = ("inst", "g", "можна обережно +20%: контролюй Direct")
        elif age is not None and age <= 3:
            if ld3 == 0 and sp3 >= 2 * (budget or 10):
                v = ("new_dead", "r", "нова РК: $%.0f за %d дн. і 0 лідів — вимкни або перезбери" % (sp3, age))
            elif cpl3 is not None and base and cpl3 <= base * 1.2:
                v = ("new_ok", "g", "нова РК працює: CPL $%.2f (норма менеджера $%.2f) — хай вчиться" % (cpl3, base))
            elif cpl3 is not None and base and cpl3 > base * 1.5 and sp3 >= (budget or 10):
                v = ("new_costly", "y", "нова РК: CPL $%.2f проти $%.2f у менеджера — дорого, глянь завтра" % (cpl3, base))
            else:
                v = ("new_test", "y", "нова РК (%d дн.) — ще тест, не чіпай" % (age or 0))
        elif (chg_date and days_at is not None and days_at >= 2 and ldA >= 5
              and cplB is not None and cplA is not None and cplA >= cplB * 1.4):
            _amt = (" ($%.0f -> $%.0f, %s)" % (chg["from"], chg["to"], _dd(chg_date))
                    if (chg or {}).get("from") and (chg or {}).get("to") else " (%s)" % _dd(chg_date))
            v = ("degrade", "r", "CPL виріс після зміни бюджету%s: $%.2f -> $%.2f — відкоти або дублюй на $10" % (_amt, cplB, cplA))
        elif days_at is not None and days_at < 2:
            v = ("wait", "y", "остання зміна %s — зачекай 2 повні дні" % _dd(since))
        elif freq is not None and freq >= 2.2:
            v = ("freq", "y", "частота %.1f — спершу свіжий креатив" % freq)
        elif (spend7 or 0) >= 25 and book7 == 0:
            v = ("no_book", "r", "$%.0f за 7 днів і 0 записів — не масштабуй" % (spend7 or 0))
        elif chk and cpa7 is not None and cpa7 >= chk * 0.25:
            v = ("cpa_high", "r", "запис $%.2f — ≥25%% чека, спершу здешевити" % cpa7)
        elif chk and cpa7 is not None and cpa7 >= chk * 0.15:
            v = ("cpa_warn", "y", "запис $%.2f — дорожче 15%% чека, не масштабуй" % cpa7)
        elif cpl7 is not None and cplM is not None and cpl7 >= cplM * 1.3:
            v = ("cpl_grow", "y", "CPL росте: $%.2f (7д) проти $%.2f (місяць) — онови креатив, не бюджет" % (cpl7, cplM))
        elif (chg_date and cplB is not None and cplA is not None and ldA >= 5
              and cplA <= cplB * 1.15 and cpa7 is not None and (book7 or 0) >= 2):
            v = ("holds", "g", "тримає після зміни (CPL $%.2f -> $%.2f) — можна ще %s" % (cplB, cplA, up))
        elif cpa7 is not None and (book7 or 0) >= 2:
            v = ("scale_ok", "g", "можна %s" % up)
        else:
            v = ("few_data", "y", "мало записів для висновку — не чіпай")
        res.append({"m": m, "id": aid, "adset": s["adset"], "campaign": s.get("campaign"),
                    "budget": s.get("budget"), "lvl": s.get("lvl"),
                    "created": created, "age": age, "chg": chg if chg_date else None,
                    "since": since, "daysAt": days_at,
                    "spend3": sp3, "leads3": ld3, "cpl3": cpl3,
                    "spend7": round(spend7 or 0, 2), "cpl7": cpl7, "cplM": cplM,
                    "cplBefore": cplB, "cplAfter": cplA, "leadsAfter": ldA,
                    "freq": freq, "book7": book7, "cpa7": cpa7, "cpaM": cpaM,
                    "unrel": unrel,
                    "verdict": {"code": v[0], "cls": v[1], "text": v[2]}})
    return res

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
    camp_days = {}   # кампанія -> {"m": менеджер, "days": {дата: [spend, leads]}} — для місячного звіту
    for r in rows:
        kind, m = classify(r.get("campaign"))
        if m is None or m not in cur["managers"]:
            continue
        d = str(r.get("date"))[:10]
        sp = round(num(r.get("spend")), 4); ld = int(num(r.get("actions_lead")))
        lead_spend[m][d] = lead_spend[m].get(d, 0.0) + sp
        lead_leads[m][d] = lead_leads[m].get(d, 0) + ld
        cn = str(r.get("campaign") or "")
        if cn:
            cd_ = camp_days.setdefault(cn, {"m": m, "days": {}}).setdefault("days", {}).setdefault(d, [0.0, 0])
            cd_[0] += sp; cd_[1] += ld

    # ліди Instagram Direct — вручну від Ірини (Meta їх не бачить як lead-екшн)
    for m, days in INST_MANUAL.items():
        if m in lead_leads:
            for d, v in days.items():
                lead_leads[m][d] = lead_leads[m].get(d, 0) + int(v.get("l", 0))

    # ЗАПОБІЖНИК: якщо Meta не віддала ЖОДНИХ витрат (22–24.08.2026: graph впав на
    # «забагато даних», а у Windsor від'єднаний FB-акаунт) — не затираємо історію
    # нулями, а лишаємо денні ряди зі старого data.json. Записи/ад-сети/креативи
    # оновлюються як завжди; у шапці сайту джерело буде позначене «-stale».
    if not any(v for days in lead_spend.values() for v in days.values()):
        kept = 0
        for m, n in cur["managers"].items():
            ds = n.get("dates") or []; sp = n.get("spend") or []; ld = n.get("leads") or []
            for i, d in enumerate(ds):
                s_ = sp[i] if i < len(sp) else 0
                l_ = ld[i] if i < len(ld) else 0
                if s_ or l_:
                    lead_spend.setdefault(m, {})[d] = s_
                    lead_leads.setdefault(m, {})[d] = l_
                    kept += 1
        if kept:
            print("!!! Meta не віддала витрат — денні ряди взято зі старого data.json (%d днів)" % kept)
            META_SOURCE["used"] += "-stale"
        else:
            print("!!! Meta не віддала витрат і в старому data.json їх теж немає")

    out = json.loads(json.dumps(cur))
    out["updated"] = datetime.datetime.now(TZ).replace(microsecond=0).isoformat()
    out["metaSource"] = META_SOURCE["used"]   # graph | windsor — видно в шапці сайту
    out["periodNote"] = "оновлюється кожні 2 год ~7:25–19:25 + ~22:25 за Прагою (вночі пауза)"
    out["factBookings"] = FACT_BOOKINGS   # факт адміністратора -> вкладка «<Місяць> (факт)»

    # ---- Кампанійний зріз (для місячного звіту керівництву): дати запуску/зупинки,
    # днів роботи, витрати/заявки по поточному й попередньому місяцях ----
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
            return {"spend": sp, "leads": ld, "days": sum(1 for d in _ds if d.startswith(pref)),
                    "cpl": round(sp / ld, 2) if ld else None}
        camps.append({"name": cn, "m": c["m"], "start": alld[0], "stop": alld[-1],
                      "active": alld[-1] >= _yst,
                      "month": _agg(_mp), "pmonth": _agg(_pp)})
    camps.sort(key=lambda x: -(x["month"]["spend"] or x["pmonth"]["spend"] or 0))
    out["campaigns"] = camps
    print("кампанійний зріз: %d кампаній (%d активних)" % (len(camps), sum(1 for c in camps if c["active"])))

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
            # Инста Париж вимкнена вручну (Ірина, 19.07); решта — авто по витратах вчора/сьогодні
            node["act"] = (m != INST_MGR_PARIS) and bool(sum(spend[-2:]) > 0)
        if m in MANAGER_OFF:
            node["act"] = False           # картка сіра і в кінці (Вика, 26.08)
        if m in MANAGER_HIDE:
            node["hide"] = True           # сайт/бриф не показують зовсім
        else:
            node.pop("hide", None)

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
                node["bookings3d"] = v.get("bookings3d", 0)
                node["bookings1d"] = v["bookings1d"]
                node["bookingsYest"] = v["bookingsYest"]
                node["bookingsMonth"] = v["bookingsMonth"]
                node["bookingsPrevMonth"] = v.get("bookingsPrevMonth", 0)
                node["q"] = v["q"]                      # lead quality per period
                # записи по днях (за датою СТВОРЕННЯ ліда) — для денних графіків CPA/записів
                _bd = {}
                for L in v.get("leadsQ", []):
                    if L.get("bk") and L.get("cd"):
                        _bd[L["cd"]] = _bd.get(L["cd"], 0) + 1
                node["bookingsDay"] = [_bd.get(d, 0) for d in node.get("dates", [])]
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
        # «знято сьогодні» — позначки, що зникли з таблиці протягом дня (скасування,
        # «уже записана», зміна статусу). Накопичується в _bookRemoved за день,
        # щоб Ірина бачила, чому «нових записів сьогодні» поменшало.
        rem_prev = cur.get("_bookRemoved") or {}
        rem_by = dict(rem_prev.get("byMgr") or {}) if rem_prev.get("date") == today_local else {}
        for m, v in bk.items():
            if m not in out["managers"] or v["leads_seen"] <= 0:
                continue
            booked_set = set(v["booked_keys"])
            mreg = dict(reg.get(m) or {})
            migrate = m not in reg
            removed_now = sum(1 for kk in mreg if kk not in booked_set)
            if removed_now:
                rem_by[m] = rem_by.get(m, 0) + removed_now
            for kk in booked_set:
                if kk not in mreg:
                    mreg[kk] = "" if migrate else today_local
            mreg = {kk: d for kk, d in mreg.items() if kk in booked_set}   # знятий запис -> геть
            new_reg[m] = mreg
            out["managers"][m]["bookingsToday"] = sum(1 for d in mreg.values() if d == today_local)
            out["managers"][m]["bookingsRemovedToday"] = rem_by.get(m, 0)
        out["_bookReg"] = new_reg
        out["_bookRemoved"] = {"date": today_local, "byMgr": rem_by}

    # Записи Instagram-карток — з INST_MANUAL (незалежно від Google-таблиці)
    _td = datetime.date.fromisoformat(TODAY)
    for m, days in INST_MANUAL.items():
        node = out["managers"].get(m)
        if node is None:
            continue
        tot = b7 = b3 = bY = bM = bPM = b1 = 0
        _ms = _td.replace(day=1); _pms = (_ms - datetime.timedelta(days=1)).replace(day=1)
        for ds, v in days.items():
            b = int(v.get("b", 0))
            if not b:
                continue
            cd = datetime.date.fromisoformat(ds)
            tot += b
            if 0 <= (_td - cd).days < 7: b7 += b
            if 0 <= (_td - cd).days < 3: b3 += b
            if cd == _td - datetime.timedelta(days=1): bY += b
            if cd >= _ms: bM += b
            if _pms <= cd < _ms: bPM += b
            if cd == _td: b1 += b
        node["bookings"] = tot; node["bookings7d"] = b7; node["bookings3d"] = b3; node["bookingsYest"] = bY
        node["bookingsMonth"] = bM; node["bookingsPrevMonth"] = bPM
        node["bookings1d"] = b1; node["bookingsToday"] = b1

    # --- Meta-метрики по періодах (CTR / CPM / частота) + ад-сети й креативи по періодах ---
    td = datetime.date.fromisoformat(TODAY)
    yest_s = (td - datetime.timedelta(days=1)).isoformat()
    d3from = (td - datetime.timedelta(days=2)).isoformat()
    d7from = (td - datetime.timedelta(days=6)).isoformat()
    mstart = td.replace(day=1).isoformat()
    _pme = td.replace(day=1) - datetime.timedelta(days=1)   # попередній календарний місяць
    _pms = _pme.replace(day=1)
    PERIODS = {"yest": (yest_s, yest_s), "d3": (d3from, TODAY), "d7": (d7from, TODAY),
               "month": (mstart, TODAY),
               "pmonth": (_pms.isoformat(), _pme.isoformat()),
               "all": (DATE_FROM, TODAY)}
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

    # ДІАГНОСТИКА атрибуції записів до ад-сетів (сайт це поле ігнорує; читаємо ми з data.json):
    # чому запис не сідає на ад-сет — телефон не в сирій вкладці, чи в сирій нема adset_id.
    if bk:
        diag = {}
        for mgr, v in bk.items():
            booked = [L for L in v.get("leadsQ", []) if L.get("bk")]
            in_raw  = sum(1 for L in booked if raw_map.get(L["ph"]))
            with_as = sum(1 for L in booked if (raw_map.get(L["ph"]) or {}).get("adset_id"))
            # d7-зріз ТОЧНО як у build_adsets: period + adset_id + hit["m"]==mgr.
            # Порівняння d7_with_adset vs d7_adset_same_m покаже, чи винна вимога збігу менеджера;
            # d7_booked vs d7_in_raw покаже, чи сира вкладка відстає по свіжих лідах.
            b7 = [L for L in booked if L.get("cd") and L["cd"] >= d7from]
            b7_in  = sum(1 for L in b7 if raw_map.get(L["ph"]))
            b7_as  = sum(1 for L in b7 if (raw_map.get(L["ph"]) or {}).get("adset_id"))
            b7_asm = sum(1 for L in b7 if (raw_map.get(L["ph"]) or {}).get("adset_id")
                         and (raw_map.get(L["ph"]) or {}).get("m") == mgr)
            diag[mgr] = {"booked": len(booked), "phone_in_raw": in_raw, "with_adset_id": with_as,
                         "raw_rows": sum(1 for h in raw_map.values() if h.get("m") == mgr),
                         "d7_booked": len(b7), "d7_in_raw": b7_in,
                         "d7_with_adset": b7_as, "d7_adset_same_m": b7_asm}
            print("  атрибуція %-6s: d7 booked %d | in_raw %d | adset %d | adset&same_m %d"
                  % (mgr, len(b7), b7_in, b7_as, b7_asm))
        out["_diag"] = {"raw_ok": raw_ok, "attribution": diag, "rawTabs": dict(RAW_STATS),
                        "sheetTabs": list(SHEET_TABS)}
        for _m, _s in RAW_STATS.items():
            print("  сира вкладка %-6s: рядків %d, останній лід %s" % (_m, _s["rows"], _s["last_lead"] or "?"))
        # аудит записів: по даті ЗАПИСУ vs по даті ЛІДА + пропущені формулювання позначок
        ba = {}
        for mgr, v in bk.items():
            a = v.get("bkAudit")
            if not a: continue
            ba[mgr] = dict(a, pm_lead=v.get("bookingsPrevMonth"), m_lead=v.get("bookingsMonth"),
                           total=v.get("bookings"))
            print("  аудит %-6s: лип по даті ЗАПИСУ %d (по даті ліда %d) | без дати %d | пропущено %d %s | ЧУЖІ ліди: лип %d (всього %d)"
                  % (mgr, a["pm_bd"], v.get("bookingsPrevMonth") or 0, a["bd_none"],
                     a["miss"], a["samples"][:3], a.get("pm_others", 0), a.get("others_bk", 0)))
        out["_diag"]["bookAudit"] = ba

    # Реєстр АДМІНІСТРАТОРА (доступ з 04.08) — авто-«факт» записів замість ручних чисел.
    # Рядки таргетолога Ірини по днях: «в запись всего» = записи менеджера за день.
    try:
        af = fetch_admin_fact()
        fact = {mk: dict(v) for mk, v in af["monthly"].items()}
        for mk, v in FACT_BOOKINGS.items():
            fact.setdefault(mk, {}).update(v)      # ручні числа мають пріоритет (липень звірений)
        out["factBookings"] = fact
        out["factDaily"] = af["daily"]             # {mgr: {"YYYY-MM-DD": записів}} — для звірок
        out.setdefault("_diag", {})["adminFact"] = {"monthly": af["monthly"], "unmapped": af["unmapped"]}
        print("  адмін-факт: місяці %s | не розпізнані кампанії: %s"
              % (sorted(af["monthly"].keys()), af["unmapped"] or "нема"))
    except Exception as e:
        print("  адмін-факт FAIL ->", str(e)[:120])

    def _merge_periods(field, fresh):
        """Свіжі періоди поверх старих: період, що не отримався, лишається зі
        старими даними (той самий принцип «ніколи не зануляти на помилці»).
        Додатково РЯДКОВИЙ перенос: якщо у свіжому рядку записи/якість не порахувались
        (CRM-вкладка менеджера не прочиталась цього разу), тягнемо їх зі старого рядка
        того ж періоду — разовий збій мосту не має затирати таблиці на «—*»."""
        if not fresh:
            print(field, "failed entirely -> carrying over old ones"); return
        prev = out.get(field)
        if isinstance(prev, dict):        # старий формат (list) просто замінюємо
            rid = ((lambda r: (r.get("m"), r.get("campaign"), r.get("adset"))) if field == "adsets"
                   else (lambda r: (r.get("m"), r.get("name"), r.get("adset"))))
            for per, rows in fresh.items():
                old = {rid(r): r for r in (prev.get(per) or []) if isinstance(r, dict)}
                n = 0
                for r in rows:
                    o = old.get(rid(r))
                    if not o: continue
                    if field == "adsets" and r.get("q") is None and o.get("q") is not None:
                        r["q"] = o["q"]; r["book"] = o.get("book"); r["cpa"] = o.get("cpa"); n += 1
                    elif field == "creatives" and r.get("book") is None and o.get("book") is not None:
                        r["book"] = o.get("book"); r["cpa"] = o.get("cpa"); r["conv"] = o.get("conv"); n += 1
                if n: print("  %s[%s]: перенесено записи/якість зі старих рядків: %d" % (field, per, n))
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

    # ---- щоденна історія ад-сетів (adsHist) — фундамент аналітики масштабування ----
    try:
        ah = build_adset_hist(cur, bk, raw_map, raw_ok)
        if ah is not None:
            out["adsHist"] = ah
            print("adsHist: %d ад-сетів, %d днів-рядків"
                  % (len(ah), sum(len(v["days"]) for v in ah.values())))
        else:
            print("adsHist: Meta нічого не віддала -> стара історія лишилась")
    except Exception as e:
        print("adsHist failed -> carrying over:", str(e)[:150])

    # ---- Журнал вимкнень (_offLog): що зникло з активних + метрики й авто-причина
    # на момент вимкнення. Для місячного звіту «що вимкнено і чому». ----
    if META_TOKEN:
        try:
            curA  = {str(x["id"]): str(x.get("name") or "") for x in _graph_all("adsets", "id,name,effective_status") if x.get("effective_status") == "ACTIVE"}
            curAd = {str(x["id"]): str(x.get("name") or "") for x in _graph_all("ads",    "id,name,effective_status") if x.get("effective_status") == "ACTIVE"}
            prevS = cur.get("_activeSnap") or {}
            offlog = list(cur.get("_offLog") or [])
            def _reason(row, chk):
                if not row: return ""
                if row.get("freq") is not None and row["freq"] >= 2.5:
                    return "частота %.1f — вигорів" % row["freq"]
                if chk and row.get("cpa") is not None and row["cpa"] >= chk * 0.25:
                    return "запис $%.0f — ≥25%% чека" % row["cpa"]
                if (row.get("spend") or 0) >= 25 and not (row.get("book") or 0):
                    return "$%.0f за 7 днів без записів" % row["spend"]
                return ""
            for typ, prev_map, cur_map, rows7, key in (
                    ("адсет",   prevS.get("adsets") or {}, curA,  out.get("adsets", {}).get("d7") or [],    "adset"),
                    ("креатив", prevS.get("ads")    or {}, curAd, out.get("creatives", {}).get("d7") or [], "name")):
                for _id, nm in prev_map.items():
                    if _id in cur_map or not nm:
                        continue
                    row = next((r for r in rows7 if r.get(key) == nm), None)
                    chk = (check_usd_node(row["m"]) or {}).get("usd") if row and row.get("m") else None
                    offlog.append({"date": TODAY, "type": typ, "name": nm,
                                   "m": row.get("m") if row else None,
                                   "spend7": row.get("spend") if row else None,
                                   "cpa7": row.get("cpa") if row else None,
                                   "freq7": row.get("freq") if row else None,
                                   "book7": row.get("book") if row else None,
                                   "reason": _reason(row, chk)})
                    print("  ВИМКНЕНО %s: %s (%s)" % (typ, nm, (row or {}).get("m")))
            out["_offLog"] = offlog[-200:]
            out["_activeSnap"] = {"adsets": curA, "ads": curAd}
        except Exception as e:
            print("offLog failed ->", str(e)[:120])

    # бюджети + історія їх змін для блоку «Масштабування» (падає м'яко, старе лишається)
    try:
        sc = fetch_scaling()
        if sc is not None:
            sc_rows, evlog = sc
            out["scaling"] = sc_rows
            print("scaling: %d активних ад-сетів, історія змін є для %d"
                  % (len(sc_rows), sum(1 for x in sc_rows if x["chg"])))
            # budgetLog: ПОВНА історія змін бюджету по object_id (журнал Meta зливаємо
            # зі збереженим — Meta може підрізати старі події). Час — пояс кабінету.
            blog = out.get("budgetLog") or {}
            for oid, evs in evlog.items():
                merged = {e["t"]: e for e in (blog.get(oid) or [])}
                for e in evs:
                    ldt = _activity_local_dt(e["t"])
                    merged[e["t"]] = {"t": e["t"],
                                      "date": ldt.date().isoformat() if ldt else str(e["t"])[:10],
                                      "time": ldt.strftime("%H:%M") if ldt else None,
                                      "from": e["old"], "to": e["new"]}
                blog[oid] = sorted(merged.values(), key=lambda x: x["t"])[-30:]
            out["budgetLog"] = blog
            print("budgetLog: %d обʼєктів, %d подій"
                  % (len(blog), sum(len(v) for v in blog.values())))
    except Exception as e:
        print("scaling failed -> carrying over:", str(e)[:150])

    # ---- життєвий цикл ад-сетів: вердикти масштабування (сайт + Telegram-бриф) ----
    try:
        lc = build_lifecycle(out)
        if lc is not None:
            out["lifecycle"] = lc
            print("lifecycle: %d рядків | %s" % (len(lc), ", ".join(
                "%s:%d" % (c, sum(1 for r in lc if r["verdict"]["code"] == c))
                for c in sorted({r["verdict"]["code"] for r in lc}))))
    except Exception as e:
        print("lifecycle failed -> carrying over:", str(e)[:150])

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

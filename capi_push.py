# -*- coding: utf-8 -*-
"""Conversions API для CRM: передає в Meta етапи лідів по Lead ID.

Набір даних «SMAS CRM» (1887772359436550). Документація Meta:
developers.facebook.com/documentation/ads-commerce/conversions-api/conversion-leads-integration
  - action_source = system_generated, custom_data.event_source = crm, lead_event_source = назва CRM
  - user_data.lead_id = 15–17 цифр (у сирих вкладках колонка `id` має префікс «l:»)
  - event_time — після створення ліда і не старше 7 днів, інакше Meta відкидає ВЕСЬ батч
  - слати всі етапи, включно з «сирим» лідом для кожної заявки; батч ≤ 1000 подій

Етапи: initial_lead (кожна заявка), booked (запис у CRM), lost (відмова в CRM).
Що вже відправлено — у capi_sent.json, щоб не слати двічі. У тестовому режимі
(CAPI_TEST_CODE) реєстр не оновлюється і подій щонайбільше TEST_LIMIT.

Змінні оточення: CAPI_TOKEN (секрет), CAPI_TEST_CODE, CAPI_ONLY (менеджер), CAPI_DRY_RUN=1.
"""
import os, re, json, time, datetime, urllib.request, urllib.parse, urllib.error
import refresh_dashboard as rd

DATASET     = "1887772359436550"
API_VERSION = "v26.0"
LEAD_SOURCE = "Google Sheets (SMAS)"
SENT_FILE   = "capi_sent.json"
TEST_LIMIT  = 20
BATCH       = 500
WIN_INITIAL = 6.5 * 86400      # запас від межі Meta у 7 днів
WIN_STAGE   = 28 * 86400       # Meta: етап воронки має статися протягом 28 днів після ліда

TOKEN     = rd._clean_secret("CAPI_TOKEN")
TEST_CODE = os.environ.get("CAPI_TEST_CODE", "").strip()
ONLY      = os.environ.get("CAPI_ONLY", "").strip()
DRY_RUN   = os.environ.get("CAPI_DRY_RUN", "").strip() == "1"

# менеджер -> сирі вкладки інтеграції (тут Lead ID) і CRM-вкладки (тут статуси)
SOURCES = {
    "Алиса": {
        "raw": [(rd.NEW_SS, "1215357648")],     # fb14 — форма 07.08.26_Alissa_newParis
        "crm": [(rd.NEW_SS, "1047608348")],     # «Алиса Ирина»
    },
}


def phone_key(v):
    d = re.sub(r"\D", "", str(v or ""))
    return d[-9:] if len(d) >= 9 else None


def lead_id(v):
    d = re.sub(r"\D", "", rd._strip_id_prefix(v))
    return int(d) if 15 <= len(d) <= 17 else None


def to_ts(v):
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(s[:24] if "%z" in fmt else s[:19], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def crm_status(r):
    if rd.is_booked(r):
        return "booked"
    s = rd._statuses(r)
    if "отказ" in s or re.search(r"не\s*удобн", s):
        return "lost"
    return None


def load_sent():
    try:
        with open(SENT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build_events(now):
    sent = load_sent()
    # Перший справжній запуск: час зміни статусу в CRM невідомий, тож для старих
    # лідів «зараз» спотворило б послідовність (Meta: модель спирається на точний
    # час, бекфіл >7 днів не підробляти). Тому на першому запуску етапи шлемо лише
    # для лідів ≤ 7 днів, а старші позначаємо відомими без відправки.
    first_run = not sent.get("_baseline")
    known_only = []          # (lead_id, етап) — позначити без відправки
    events, stats = [], {}
    for mgr, src in SOURCES.items():
        if ONLY and mgr != ONLY:
            continue
        status = {}
        for ss, gid in src["crm"]:
            for r in rd.fetch_sheet_rows(ss=ss, gid=gid):
                k = phone_key(r.get("phone_number") or r.get("номер"))
                st = crm_status(r)
                if k and st and status.get(k) != "booked":   # запис важливіший за відмову
                    status[k] = st
        st_mgr = {"leads": 0, "bad_id": 0, "initial_lead": 0, "booked": 0, "lost": 0, "too_old": 0}
        for ss, gid in src["raw"]:
            for r in rd.fetch_sheet_rows(ss=ss, gid=gid):
                lid = lead_id(r.get("id"))
                created = to_ts(r.get("created_time"))
                if not created:
                    continue
                st_mgr["leads"] += 1
                if not lid:
                    st_mgr["bad_id"] += 1
                    continue
                done = set(sent.get(str(lid), []))
                age = now - created
                todo = []
                if "initial_lead" not in done and age <= WIN_INITIAL:
                    todo.append(("initial_lead", created + 60))
                stage = status.get(phone_key(r.get("phone_number")))
                if stage and stage not in done:
                    limit = WIN_INITIAL if first_run else WIN_STAGE
                    if age <= limit:
                        todo.append((stage, now))
                    else:
                        st_mgr["too_old"] += 1
                        known_only.append((lid, stage))
                for name, t in todo:
                    st_mgr[name] += 1
                    events.append({
                        "event_name": name,
                        "event_time": int(t),
                        "action_source": "system_generated",
                        "user_data": {"lead_id": lid},
                        "custom_data": {"event_source": "crm", "lead_event_source": LEAD_SOURCE},
                    })
        stats[mgr] = st_mgr
    return events, stats, sent, known_only, first_run


def post(batch):
    url = "https://graph.facebook.com/%s/%s/events" % (API_VERSION, DATASET)
    body = {"data": json.dumps(batch), "access_token": TOKEN}
    if TEST_CODE:
        body["test_event_code"] = TEST_CODE
    req = urllib.request.Request(url, data=urllib.parse.urlencode(body).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return False, {"error": {"message": "HTTP %s" % e.code}}


def main():
    now = int(time.time())
    events, stats, sent, known_only, first_run = build_events(now)
    for mgr, s in stats.items():
        print("%-8s лідів %d | без валідного Lead ID %d | initial_lead %d | booked %d | lost %d | етап поза вікном (не шлемо) %d"
              % (mgr, s["leads"], s["bad_id"], s["initial_lead"], s["booked"], s["lost"], s["too_old"]))
    if first_run:
        print("перший запуск: етапи лише для лідів ≤ 7 днів, ще %d позначаю відомими без відправки" % len(known_only))
    if TEST_CODE:
        # у тесті — по кілька подій кожного типу, щоб було видно всі етапи
        pick, seen = [], {}
        for e in events:
            n = e["event_name"]
            if seen.get(n, 0) < TEST_LIMIT // 3 + 1:
                pick.append(e); seen[n] = seen.get(n, 0) + 1
        events = pick[:TEST_LIMIT]
        print("ТЕСТОВИЙ РЕЖИМ (%s): відправляю %d подій, реєстр не змінюється" % (TEST_CODE, len(events)))
    if DRY_RUN or not events:
        print("DRY RUN / нічого відправляти" if DRY_RUN else "нових подій немає")
        return
    if not TOKEN:
        raise SystemExit("CAPI_TOKEN не заданий")
    ok_total = 0
    for i in range(0, len(events), BATCH):
        batch = events[i:i + BATCH]
        ok, resp = post(batch)
        if ok:
            ok_total += int(resp.get("events_received") or 0)
            print("батч %d: прийнято %s | fbtrace_id %s | %s"
                  % (i // BATCH + 1, resp.get("events_received"), resp.get("fbtrace_id"),
                     "; ".join(resp.get("messages") or []) or "без повідомлень"))
            if not TEST_CODE:
                for e in batch:
                    k = str(e["user_data"]["lead_id"])
                    sent.setdefault(k, [])
                    if e["event_name"] not in sent[k]:
                        sent[k].append(e["event_name"])
        else:
            err = resp.get("error") or {}
            print("батч %d ВІДХИЛЕНО: %s | %s | fbtrace_id %s"
                  % (i // BATCH + 1, err.get("message"), err.get("error_user_msg") or "",
                     err.get("fbtrace_id")))
    if not TEST_CODE:
        for lid, stage in known_only:
            lst = sent.setdefault(str(lid), [])
            if stage not in lst:
                lst.append(stage)
        sent["_baseline"] = True
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(sent, f, ensure_ascii=False, separators=(",", ":"))
    print("разом прийнято Meta: %d" % ok_total)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""Conversions API для CRM: передає в Meta етапи лідів по Lead ID.

Набір даних «SMAS CRM» (1887772359436550). Документація Meta:
developers.facebook.com/documentation/ads-commerce/conversions-api/conversion-leads-integration
  - action_source = system_generated, custom_data.event_source = crm, lead_event_source = назва CRM
  - user_data.lead_id = 15–17 цифр (у сирих вкладках колонка `id` має префікс «l:»)
  - event_time — після створення ліда і не старше 7 днів, інакше Meta відкидає ВЕСЬ батч
  - слати всі етапи, включно з «сирим» лідом для кожної заявки; батч ≤ 1000 подій
  - перевірка інтеграції: Lead Coverage (частка лідів із подіями) ≥ 60% — тому шлемо
    ВСІ ліди кабінету 1, а не лише тестового менеджера

Етапи: initial_lead (кожна заявка), booked (запис у CRM), lost (відмова в CRM).
Ліди беремо з тих самих сирих вкладок і з тим самим менеджером, що й дашборд
(rd.fetch_raw_map -> rd.RAW_LEADS), статуси — з CRM-вкладок менеджерів (як compute_bookings).
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

# Менеджери кабінету 1 (873265084670144) — набір даних підключено до нього.
# НВ (інший кабінет), Барселона, Вика та інста-картки сюди не входять.
CAB1 = ("Алиса", "Таня", "Сюзанна", "Карина", "Саида", "Даша", "Диана", "Мага", "Даник")
# РК переходили між цими менеджерами (rd.CAMP_MOVE) — статус ліда шукаємо в CRM усієї групи
MOVE_GROUP = ("Мага", "Даник", "Сюзанна")


def lead_id(v):
    d = re.sub(r"\D", "", rd._strip_id_prefix(v))
    return int(d) if 15 <= len(d) <= 17 else None


def to_ts(v):
    """created_time сирих вкладок «2026-09-05T19:03:00-05:00» -> unix-час із урахуванням поясу."""
    s = str(v or "").strip()
    if not s:
        return None
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s.replace("Z", "+00:00"))
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def crm_status(r):
    if rd.is_booked(r):
        return "booked"
    s = rd._statuses(r)
    if "отказ" in s or re.search(r"не\s*удобн", s):
        return "lost"
    return None


def crm_rows(mgr):
    """Усі CRM-рядки менеджера: основна вкладка + додаткові (як rd.compute_bookings)."""
    refs = []
    if mgr in rd.MANAGER_GID:
        refs.append(rd._sheet_args(rd.MANAGER_GID[mgr]))
    for extra in rd.MANAGER_EXTRA_SHEETS.get(mgr, ()):
        if isinstance(extra, (tuple, list)):
            ss, ref = extra
            refs.append({"ss": ss, "gid": ref} if str(ref).isdigit() else {"ss": ss, "sheet": ref})
        else:
            refs.append({"sheet": extra})
    rows = []
    for kw in refs:
        try:
            rows += [rd._remap_new_row(dict(r)) for r in rd.fetch_sheet_rows(**kw)]
        except Exception as e:
            print("  CRM-вкладка %s (%s) FAIL -> %s" % (kw, mgr, str(e)[:80]))
    return rows


def crm_statuses(mgrs):
    """{менеджер: {phone9: 'booked'|'lost'}}; запис важливіший за відмову."""
    out = {}
    for mgr in mgrs:
        st = out.setdefault(mgr, {})
        for r in crm_rows(mgr):
            if not rd._iryna_row(r):
                continue
            k = rd.phone9(r.get("phone_number"))
            s = crm_status(r)
            if k and s and st.get(k) != "booked":
                st[k] = s
    return out


def stage_of(mgr, phone, status):
    group = MOVE_GROUP if mgr in MOVE_GROUP else (mgr,)
    found = {status.get(m, {}).get(phone) for m in group}
    return "booked" if "booked" in found else ("lost" if "lost" in found else None)


def own_adsets():
    """ID ад-сетів Ірини = ключі adsHist у data.json (дашборд бере їх із її кабінетів).
    Сирі вкладки містять і ліди РК Вери (new:fb11 — її РК на Дашу, у назві теж «Dasha»),
    тож без цього фільтра в набір даних пішли б чужі ліди."""
    try:
        with open(rd.DATA_FILE, encoding="utf-8") as f:
            return set((json.load(f).get("adsHist") or {}).keys())
    except (OSError, ValueError):
        return set()


def raw_leads(now):
    """{lead_id: {m, created, phone}} — ліди кабінету 1 із сирих вкладок.
    Друге значення — {менеджер: {"bad_id": N, "foreign": N}} за 7 днів, для звіту."""
    own = own_adsets()
    if not own:
        print("УВАГА: data.json без adsHist — не можу відрізнити ліди Ірини, нічого не шлю")
    rd.fetch_raw_map()
    leads, skip = {}, {}
    for x in rd.RAW_LEADS:
        if x["tab"].startswith("nb:") or x["m"] not in CAB1:   # nb: — вкладки кабінету НВ
            continue
        if ONLY and x["m"] != ONLY:
            continue
        created = to_ts(x["created"])
        if not created:
            continue
        sk = skip.setdefault(x["m"], {"bad_id": 0, "foreign": 0})
        recent = now - created <= 7 * 86400
        if x["adset_id"] not in own:       # чужа РК або Meta не віддала adset_id
            sk["foreign"] += recent
            continue
        lid = lead_id(x["id"])
        if not lid:
            sk["bad_id"] += recent
            continue
        leads.setdefault(lid, {"m": x["m"], "created": created, "phone": x["phone"]})
    return leads, skip


def load_sent():
    try:
        with open(SENT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build_events(now):
    sent = load_sent()
    # Перший справжній запуск МЕНЕДЖЕРА: час зміни статусу в CRM невідомий, тож для
    # старих лідів «зараз» спотворило б послідовність (Meta: модель спирається на точний
    # час, бекфіл >7 днів не підробляти). Тому на першому запуску етапи шлемо лише для
    # лідів ≤ 7 днів, а старші позначаємо відомими без відправки.
    # «_baseline: true» — формат до 05.10, коли слали тільки Алису.
    based = set(sent.get("_baselines") or (["Алиса"] if sent.get("_baseline") else []))
    leads, skip = raw_leads(now)
    status = crm_statuses(sorted({x["m"] for x in leads.values()} | set(MOVE_GROUP)))
    known_only = []          # (lead_id, етап) — позначити без відправки
    events, stats = [], {}
    for lid, x in sorted(leads.items(), key=lambda kv: kv[1]["created"]):
        mgr, created = x["m"], x["created"]
        age = now - created
        if age > WIN_STAGE:
            continue                       # поза вікном Meta — подій для нього вже не буде
        s = stats.setdefault(mgr, {"leads": 0, "initial_lead": 0, "booked": 0, "lost": 0, "too_old": 0})
        s["leads"] += 1
        done = set(sent.get(str(lid), []))
        todo = []
        if "initial_lead" not in done and age <= WIN_INITIAL:
            todo.append(("initial_lead", min(created + 60, now)))
        stage = stage_of(mgr, x["phone"], status)
        if stage and stage not in done:
            limit = WIN_STAGE if mgr in based else WIN_INITIAL
            if age <= limit:
                todo.append((stage, now))
            else:
                s["too_old"] += 1
                known_only.append((lid, stage))
        for name, t in todo:
            s[name] += 1
            events.append({
                "event_name": name,
                "event_time": int(t),
                "action_source": "system_generated",
                "user_data": {"lead_id": lid},
                "custom_data": {"event_source": "crm", "lead_event_source": LEAD_SOURCE},
            })
    for mgr, sk in skip.items():
        stats.setdefault(mgr, {"leads": 0, "initial_lead": 0, "booked": 0, "lost": 0, "too_old": 0}).update(sk)
    # реєстр не росте вічно: ліди, старші за вікно етапів, уже нічого не отримають
    stale = [str(lid) for lid, x in leads.items() if now - x["created"] > WIN_STAGE + 86400]
    return events, stats, sent, known_only, based, stale


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
    events, stats, sent, known_only, based, stale = build_events(now)
    for mgr in sorted(stats):
        s = stats[mgr]
        print("%-8s лідів за 28 дн %d | за 7 дн: чужих/без ад-сету %d, без Lead ID %d | initial_lead %d | booked %d | lost %d | старий етап (не шлемо) %d%s"
              % (mgr, s["leads"], s.get("foreign", 0), s.get("bad_id", 0), s["initial_lead"], s["booked"],
                 s["lost"], s["too_old"], "" if mgr in based else " | перший запуск"))
    if known_only:
        print("перший запуск менеджерів: %d старих етапів позначаю відомими без відправки" % len(known_only))
    if TEST_CODE:
        # у тесті — по кілька подій кожного типу, щоб було видно всі етапи
        pick, seen = [], {}
        for e in events:
            n = e["event_name"]
            if seen.get(n, 0) < TEST_LIMIT // 3 + 1:
                pick.append(e); seen[n] = seen.get(n, 0) + 1
        events = pick[:TEST_LIMIT]
        print("ТЕСТОВИЙ РЕЖИМ (%s): відправляю %d подій, реєстр не змінюється" % (TEST_CODE, len(events)))
    print("до відправки: %d подій" % len(events))
    if DRY_RUN or not events:
        print("DRY RUN — нічого не відправляю" if DRY_RUN else "нових подій немає")
        return
    if not TOKEN:
        raise SystemExit("CAPI_TOKEN не заданий")
    ok_total, failed = 0, 0
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
                    lst = sent.setdefault(str(e["user_data"]["lead_id"]), [])
                    if e["event_name"] not in lst:
                        lst.append(e["event_name"])
        else:
            failed += 1
            err = resp.get("error") or {}
            print("батч %d ВІДХИЛЕНО: %s | %s | fbtrace_id %s"
                  % (i // BATCH + 1, err.get("message"), err.get("error_user_msg") or "",
                     err.get("fbtrace_id")))
    if not TEST_CODE:
        for lid, stage in known_only:
            lst = sent.setdefault(str(lid), [])
            if stage not in lst:
                lst.append(stage)
        # базовий запуск нових менеджерів зараховуємо лише тоді, коли всі батчі пройшли
        sent["_baselines"] = sorted(based | (set() if failed else set(stats)))
        sent.pop("_baseline", None)
        for k in stale:
            sent.pop(k, None)
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(sent, f, ensure_ascii=False, separators=(",", ":"))
    print("разом прийнято Meta: %d" % ok_total)


if __name__ == "__main__":
    main()

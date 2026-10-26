#!/usr/bin/env python3
"""Вечірній Telegram-бриф по кабінету ad5: Стомат Київ + Стомат Квіз, СМАС Відень/Київ/Тернопіль.

Запускається з GitHub Actions після кожного «Refresh vena dashboard (ad5)»
(workflow_run), але шле ЛИШЕ раз на день — після першого вечірнього оновлення (з 22:00
за Києвом) — разом зі SMAS-брифом. Кабінет живе за Києвом, тож звітуємо
«за сьогодні» станом на ~22–23:00 (без останньої години
дня — різниця мізерна). Ручний запуск (workflow_dispatch) шле завжди.

Дані вже пораховані дашбордом (vena/data.json) — тут нічого не тягнеться з Meta.
Секрет (GitHub Actions): TELEGRAM_BOT_TOKEN — токен бота @ad_smas_lifting_bot.
"""
import math
import os, json, time, datetime, urllib.request, urllib.parse, sys
from zoneinfo import ZoneInfo

CHAT_ID  = "901823018"
TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DATA_URL = "https://raw.githubusercontent.com/IrynaSyvashchenko/smas-dashboard/main/vena/data.json"
DASH_URL = "https://irynasyvashchenko.github.io/smas-dashboard/vena/"
# дата останнього відправленого брифа (workflow комітить файл) — раз на день, а не
# після кожного вечірнього оновлення (було по 2–4 на вечір, Ірина 27.10)
SENT_FILE = "brief_sent_vena.txt"

def local_dt(upd):
    """Час оновлення за КИЄВОМ з урахуванням літнього/зимового часу (фіксований +3
    після 25.10 зсував добу на годину)."""
    dt = datetime.datetime.fromisoformat(upd)
    try:
        return dt.astimezone(ZoneInfo("Europe/Kyiv"))
    except Exception:
        return dt

def sent_day():
    try:
        return open(SENT_FILE, encoding="utf-8").read().strip()
    except OSError:
        return ""

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vena-brief", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=90).read()

def send(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text,
                                   "disable_web_page_preview": "true"}).encode("utf-8")
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30).read()

def build(d, use_today=True):
    upd = d["updated"]
    try:
        ut = datetime.datetime.fromisoformat(upd)
        if (datetime.datetime.now(datetime.timezone.utc) - ut).total_seconds() > 15 * 3600:
            return "⚠️ Дашборд ad5 не оновлюється з %s, перевір GitHub Actions" % upd
    except Exception:
        pass

    # Звітний день: увечері = сьогодні (день, що закривається, дані до ~23:00);
    # вранішнє надолуження / ручний ранковий запуск = учора (повний день).
    today = local_dt(upd).date()
    rday  = today if use_today else today - datetime.timedelta(days=1)
    rs    = rday.isoformat()

    M = d["managers"]
    rows = []; tsp = tld = tbk = 0
    for m, n in M.items():
        try:
            i = n["dates"].index(rs)
        except (KeyError, ValueError):
            continue
        sp = n["spend"][i] or 0; ld = n["leads"][i] or 0
        bk = (n.get("bookingsToday") if use_today else n.get("bookingsYest")) or 0
        if sp <= 0 and ld <= 0 and not bk:
            continue
        tsp += sp; tld += ld; tbk += bk
        rows.append((m, sp, ld, bk))
    rows.sort(key=lambda r: -r[1])

    cpa = ("$%.2f" % (tsp / tbk)) if tbk else "—"
    hdr = "сьогодні" if use_today else "вчора"
    L = ["🦷 Кабінет ad5 за %s (%s)" % (hdr, rday.strftime("%d.%m")),
         "Разом: $%.0f · %d лідів · %d записів · CPA %s" % (tsp, tld, tbk, cpa), ""]
    for m, sp, ld, bk in rows:
        cpl = (" (лід $%.2f)" % (sp / ld)) if ld else ""
        cpb = (" (запис $%.2f)" % (sp / bk)) if bk else ""
        L.append("%s: $%.0f · %d л%s · %d з%s" % (m, sp, ld, cpl, bk, cpb))

    # ---- норми напрямків, як у дашборді /vena/ (з 20.10.2026; пороги від чека/$35 прибрано) ----
    yest = today - datetime.timedelta(days=1)
    def pcdf(k, lam):
        q = math.exp(-lam); s = q
        for i in range(1, int(k) + 1):
            q *= lam / i; s += q
        return min(1.0, s)
    def sig(p):
        if p < 0.05: return "різниця реальна (випадковість %s%%)" % ("<1" if p < 0.01 else round(p * 100))
        if p < 0.2:  return "слабкий сигнал (%d%%), під наглядом" % round(p * 100)
        return "мало даних (%d%%), під наглядом" % round(p * 100)
    def crm_norm(m):
        n = M.get(m) or {}; bd = n.get("bookingsDay")
        if not isinstance(bd, list): return None
        lo = (yest - datetime.timedelta(days=30)).isoformat(); hi = (yest - datetime.timedelta(days=2)).isoformat()
        sp = ld = bk = 0
        for i, dt_ in enumerate(n.get("dates") or []):
            if lo <= dt_ <= hi:
                sp += n["spend"][i] or 0; ld += n["leads"][i] or 0; bk += bd[i] or 0
        return {"cpa": sp / bk, "cpl": sp / ld, "conv": bk / ld} if bk >= 5 and ld else None
    def over_norm(o):
        """Ад-сет/креатив: запис ≥1.5× CRM-норми свого напрямку при витратах ≥2× норми -> рядок або None."""
        nn = crm_norm(o["m"])
        if not nn or (o.get("spend") or 0) < 2 * nn["cpa"]: return None
        bk = o.get("book") or 0; cpa = (o["spend"] / bk) if bk else float("inf")
        if cpa < 1.5 * nn["cpa"]: return None
        p = pcdf(bk, (o.get("leads") or 0) * nn["conv"])
        return ("🔴" if p < 0.05 else "🟡", "запис %s (%d з %d лідів) проти норми $%.2f — %s"
                % (("$%.2f" % cpa) if bk else "—", bk, o.get("leads") or 0, nn["cpa"], sig(p)))

    def days_live(m):
        s = M.get(m, {}).get("start")
        return ((today - datetime.date.fromisoformat(s)).days + 1) if s else 99

    al, dead, flagged = [], set(), set()
    for a in (d.get("adsets", {}).get("yest") or []):
        if a.get("act") is False:
            continue
        if (a.get("spend") or 0) >= 5 and (a.get("leads") or 0) == 0:
            dead.add(a["adset"])
            al.append("• %s (%s): $%.2f вчора без лідів — глянь або вимкни" % (a["adset"], a["m"], a["spend"]))
    for a in (d.get("adsets", {}).get("d7") or []):
        m = a["m"]
        if a.get("act") is False or a["adset"] in dead or days_live(m) < 4:
            continue
        dl = days_live(m)
        per = ("%d дн." % dl) if dl < 7 else "7 днів"
        r = over_norm(a)
        if r:
            flagged.add((m, a["adset"]))
            al.append("%s %s (%s), %s: %s" % (r[0], a["adset"], m, per, r[1]))
    # креатив, що за 7 днів з'їв $15+ і не дав жодного запису (ловить слабкі відео
    # всередині робочого ад-сета, як quiz_video1)
    for c in (d.get("creatives", {}).get("d7") or []):
        if c.get("act") is False or c.get("adset") in dead:
            continue
        if (c["m"], c.get("adset")) in flagged:
            continue   # уже сказано на рівні ад-сета
        r = over_norm(c)
        if r:
            al.append("%s Креатив %s (%s): %s" % (r[0], c["name"], c["m"], r[1]))
    for c in (d.get("creatives", {}).get("yest") or []):
        if c.get("act") is False or c.get("adset") in dead:
            continue
        if c.get("freq") is not None and c["freq"] >= 2.5:
            al.append("• Креатив %s (%s): частота %.2f — вигорів" % (c["name"], c["m"], c["freq"]))
    L.append("")
    if al:
        L.append("🔴 Що змінити:" if any(x.startswith(("🔴", "•")) for x in al) else "🟡 Дорожче за норму (під наглядом):"); L += al
    else:
        L.append("✅ Все спокійно: гроші без лідів не горять, записи не задорогі.")

    d7map = {(a["m"], a["adset"]): a for a in (d.get("adsets", {}).get("d7") or [])}
    sc = []
    for s in (d.get("scaling") or []):
        since = (s.get("chg") or {}).get("date") or s.get("created")
        if not since or (today - datetime.date.fromisoformat(since)).days < 2:
            continue
        a = d7map.get((s["m"], s["adset"]))
        if not a or (a.get("book") or 0) < 2:
            continue
        if a.get("freq") is not None and a["freq"] >= 2.2:
            continue
        nn = crm_norm(s["m"])
        if a.get("cpa") is not None and nn and a["cpa"] >= nn["cpa"]:
            continue
        b = s.get("budget") or 0
        sc.append("• %s (%s): $%.0f → $%.2f" % (s["adset"], s["m"], b, b * 1.25))
    if sc:
        L.append(""); L.append("🚀 Можна масштабувати сьогодні:"); L += sc

    L.append(""); L.append("📊 Дашборд: %s" % DASH_URL)
    return "\n".join(L)

def token_warn(d):
    """Рядок-попередження про токен Meta на початок брифа (порожній, якщо все гаразд)."""
    tk = d.get("metaToken") or {}
    src = str(d.get("metaSource") or "")
    if tk.get("valid") is False or "stale" in src:
        return "🔑 Meta не віддає дані — токен протух або не відповідає. Витрати/ліди нижче застиглі. Онови META_TOKEN у GitHub Secrets.\n\n"
    dl = tk.get("daysLeft")
    if dl is not None and dl <= 7:
        return "🔑 Токен Meta закінчується %s (через %d дн.) — онови заздалегідь.\n\n" % (str(tk.get("expires") or "")[:10], dl)
    return ""

def adsquiz_warn(d):
    """Залишок лідів плану ADSQuiz: <=3 — час переставити квіз-оголошення на новий квіз."""
    aq = d.get("adsquiz") or {}
    left = aq.get("left")
    if left is None:
        return ""
    if left <= 3:
        return "🧩 На ADSQuiz лишилось %d лід(ів) з плану — ПЕРЕСТАВЛЯЙ квіз-оголошення на %s (скажи Клоду «переставити квіз»).\n\n" % (left, aq.get("newUrl") or "")
    if left <= 8:
        return "🧩 На ADSQuiz лишилось %d лідів з плану — скоро переставляти квіз на %s.\n\n" % (left, aq.get("newUrl") or "")
    return ""

def datacheck_warn(d):
    """Напрямки, де ліди CRM за 7 днів не привʼязуються до оголошень (сира вкладка замерзла)."""
    dc = d.get("dataCheck") or {}
    # без rawLast = вкладка не прочиталась саме в цьому запуску (тимчасово) — у бриф не тягнемо
    bad = [(m, c) for m, c in dc.items() if c.get("leads7") and not c.get("ok") and c.get("rawLast")]
    if not bad:
        return ""
    parts = ["%s (%d з %d лідів, вкладка з %s)" % (m, c.get("matched7", 0), c.get("leads7", 0),
             str(c.get("rawLast") or "?")[:10]) for m, c in bad]
    return "🧩 Дані по ад-сетах/креативах неповні: " + "; ".join(parts) + ". Перепідключи експорт форми Meta -> Google Таблиці.\n\n"

def main():
    if not TOKEN:
        sys.exit("ERROR: TELEGRAM_BOT_TOKEN не заданий (додай секрет у GitHub Actions)")
    d = json.loads(_get(DATA_URL + "?cb=%d" % int(time.time())).decode("utf-8"))
    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    lt = local_dt(d.get("updated", ""))
    # РАЗ НА ДЕНЬ: перше оновлення з 22:00 за Києвом (= 21:00 Праги, разом зі SMAS-брифом)
    # -> «за сьогодні»; оновлення, завезене за північ (00..03), надолужує «за вчора», якщо
    # той бриф ще не йшов. Ручний запуск: після обіду «сьогодні», зранку «вчора», не мітить.
    if lt.hour >= 22:
        day, use_today = lt.date(), True
    elif lt.hour < 4:
        day, use_today = lt.date() - datetime.timedelta(days=1), False
    else:
        day, use_today = None, lt.hour >= 12
    if not manual:
        if day is None:
            print("skip: not the evening refresh (Kyiv %s)" % lt.strftime("%H:%M"))
            return
        if sent_day() == day.isoformat():
            print("skip: brief for %s already sent" % day)
            return
    text = token_warn(d) + adsquiz_warn(d) + datacheck_warn(d) + build(d, use_today=use_today)
    send(text)
    print("sent, %d chars" % len(text))
    if not manual:
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            f.write(day.isoformat() + "\n")

if __name__ == "__main__":
    main()

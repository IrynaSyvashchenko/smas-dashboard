#!/usr/bin/env python3
"""Вечірній Telegram-бриф по Meta-рекламі «SMAS-ліфтинг».

Запускається з GitHub Actions ОДРАЗУ ПІСЛЯ оновлення даних (workflow_run після
«Refresh dashboard data») — у хмарі, незалежно від комп'ютера Ірини. Шле лише після
ВЕЧІРНЬОГО оновлення (година updated >= 21), коли дані за день готові; після денних
оновлень main() тихо виходить. Читає свіжий data.json з raw.githubusercontent (main),
збирає короткий звіт за день і шле в Telegram через Bot API.

Дані вже пораховані дашбордом — тут нічого не тягнеться з Meta.
Секрет (GitHub Actions): TELEGRAM_BOT_TOKEN — токен бота @ad_smas_lifting_bot.
"""
import os, json, time, datetime, urllib.request, urllib.parse, sys

CHAT_ID  = "901823018"
TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DATA_URL = "https://raw.githubusercontent.com/IrynaSyvashchenko/smas-dashboard/main/data.json"

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "smas-brief", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=90).read()

def send(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    # Telegram ріже повідомлення на 4096 символів — довгий бриф шлемо частинами по рядках
    parts, buf = [], ""
    for ln in text.split("\n"):
        if len(buf) + len(ln) + 1 > 3900:
            parts.append(buf); buf = ln
        else:
            buf = (buf + "\n" + ln) if buf else ln
    if buf:
        parts.append(buf)
    for p in parts:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": p,
                                       "disable_web_page_preview": "true"}).encode("utf-8")
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30).read()

def build(d):
    upd = d["updated"]
    # свіжість: якщо дані старіші за 15 годин — короткe попередження замість звіту
    try:
        ut = datetime.datetime.fromisoformat(upd)
        if (datetime.datetime.now(datetime.timezone.utc) - ut).total_seconds() > 15 * 3600:
            return "⚠️ Дашборд не оновлюється з %s, перевір GitHub Actions" % upd
    except Exception:
        pass

    # звітний день: увечері (година updated >= 21) = день, що тільки-но закрився;
    # інакше (вранішнє надолуження) = попередній повний день.
    hour  = int(upd[11:13])
    ud    = datetime.date.fromisoformat(upd[:10])
    today = ud
    if hour >= 21:
        rday, use1d = ud, True
    else:
        rday, use1d = ud - datetime.timedelta(days=1), False
    rs = rday.isoformat()

    M = d["managers"]
    _hid = lambda m: bool((M.get(m) or {}).get("hide"))   # приховані (Юлиана, інста)
    rows = []; tsp = tld = tbk = 0
    for m, n in M.items():
        if n.get("act") is False or n.get("hide"):
            continue
        try:
            i = n["dates"].index(rs)
        except ValueError:
            continue
        sp = n["spend"][i]; ld = n["leads"][i]
        # записи = ті самі, що зверху на дашборді:
        #   увечері (звітний = сьогодні) -> bookingsToday (нові позначки запису за сьогодні);
        #   вранішнє надолуження (звітний = вчора) -> bookingsYest.
        if use1d:
            bk = n.get("bookingsToday")
            if bk is None:
                bk = (n.get("bookings1d") or 0) + (n.get("bookingsOld1d") or 0)
        else:
            bk = n.get("bookingsYest")
        bk = bk or 0
        # показуємо менеджера, якщо є витрати, ліди АБО записи — інакше той, у кого
        # день без відкрутки (пауза/перейменована РК), випадав би зі звіту зовсім
        if sp <= 0 and ld <= 0 and not bk:
            continue
        tsp += sp; tld += ld; tbk += bk
        rows.append((m, sp, ld, bk))
    rows.sort(key=lambda r: -r[1])

    cpa = ("$%.2f" % (tsp / tbk)) if tbk else "—"
    hdr = "сьогодні" if use1d else "вчора"
    L = ["☀️ SMAS за %s (%s)" % (hdr, rday.strftime("%d.%m")),
         "Разом: $%.0f · %d лідів · %d записів (нові позначки за день) · CPA %s" % (tsp, tld, tbk, cpa), ""]
    for m, sp, ld, bk in rows:
        L.append("%s: $%.0f · %d л · %d з" % (m, sp, ld, bk))

    def days_live(m):
        s = M.get(m, {}).get("start")
        return ((today - datetime.date.fromisoformat(s)).days + 1) if s else 99

    def chk(m):
        return (M.get(m, {}).get("check") or {}).get("usd")

    al, dead = [], set()
    for a in (d.get("adsets", {}).get("yest") or []):
        if str(a["m"]).startswith("Инста") or a.get("act") is False:
            continue
        if (a.get("spend") or 0) >= 5 and (a.get("leads") or 0) == 0:
            dead.add(a["adset"])
            al.append("• %s (%s): $%.2f без лідів — глянь або вимкни" % (a["adset"], a["m"], a["spend"]))
    # CPA по АД-СЕТАХ за 7 днів (прив'язку записів до ад-сета полагоджено — див. refresh_dashboard).
    # Запобіжник: якщо на ад-сети менеджера сіло <60% його записів за 7 днів
    # (сира вкладка відстає) — адсет-CPA завищена, такі алерти не шлемо.
    as_bk7 = {}
    for a in (d.get("adsets", {}).get("d7") or []):
        if not str(a["m"]).startswith("Инста"):
            as_bk7[a["m"]] = as_bk7.get(a["m"], 0) + (a.get("book") or 0)
    def _unrel(mm):
        c = M.get(mm, {}).get("bookings7d") or 0
        return c >= 5 and as_bk7.get(mm, 0) < c * 0.6
    for a in (d.get("adsets", {}).get("d7") or []):
        m = a["m"]
        if str(m).startswith("Инста") or a.get("act") is False or a["adset"] in dead or _unrel(m):
            continue
        dl = days_live(m)
        if dl < 4:
            continue
        per = ("%d дн." % dl) if dl < 7 else "7 днів"
        ck, cp = chk(m), a.get("cpa")
        if (a.get("spend") or 0) >= 25 and (a.get("book") or 0) == 0:
            al.append("• %s (%s): $%.0f за %s і 0 записів" % (a["adset"], m, a["spend"], per))
        elif ck and cp is not None and cp >= ck * 0.25:
            al.append("• %s (%s): запис $%.2f (%s) — дуже дорого" % (a["adset"], m, cp, per))
        elif ck and cp is not None and cp >= ck * 0.15:
            al.append("• %s (%s): запис $%.2f (%s) — дорожче 15%% чека" % (a["adset"], m, cp, per))
    for c in (d.get("creatives", {}).get("yest") or []):
        if c.get("act") is False or c.get("adset") in dead:
            continue
        if c.get("freq") is not None and c["freq"] >= 2.5:
            al.append("• Креатив %s (%s): частота %.2f — вигорів" % (c["name"], c["m"], c["freq"]))
    # алерти ДИНАМІКИ з життєвого циклу (рахує пайплайн, та сама логіка, що на сайті):
    # CPL зламався після зміни бюджету / нова РК горить без лідів
    LC = [r for r in (d.get("lifecycle") or []) if not _hid(r.get("m"))]
    for r in LC:
        v = r.get("verdict") or {}
        if v.get("code") == "degrade":
            al.append("• %s (%s): %s" % (r["adset"], r["m"], v.get("text")))
        elif v.get("code") == "new_dead" and r["adset"] not in dead:
            al.append("• %s (%s): %s" % (r["adset"], r["m"], v.get("text")))
    L.append("")
    if al:
        L.append("🔴 Потребує уваги:"); L += al
    else:
        L.append("✅ Все спокійно: гроші без лідів не горять, записи не задорогі.")

    _dd = lambda s: "%s.%s" % (s[8:10], s[5:7]) if s and len(str(s)) >= 10 else "?"

    # 🆕 нові РК (≤3 днів): швидкий пульс — працює / дорого / рано судити
    news = []
    for r in LC:
        code = str((r.get("verdict") or {}).get("code") or "")
        if not code.startswith("new"):
            continue
        cpl = r.get("cpl3")
        icon = {"new_ok": "✅", "new_costly": "⚠️", "new_dead": "🔴"}.get(code, "⏳")
        news.append("%s %s (%s): $%.0f/д · %d дн. · CPL %s" % (
            icon, r["adset"], r["m"], r.get("budget") or 0, r.get("age") or 0,
            ("$%.2f" % cpl) if cpl is not None else "—"))
    if news:
        L.append(""); L.append("🆕 Нові РК:"); L += news

    # 📈 масштабовані за останній тиждень: тримає чи деградує (CPL до → після)
    chgd = []
    for r in LC:
        ch = r.get("chg") or {}
        if not ch.get("date") or (r.get("age") or 99) <= 3:
            continue
        try:
            if (today - datetime.date.fromisoformat(ch["date"])).days > 7:
                continue
        except Exception:
            continue
        b_, a_ = r.get("cplBefore"), r.get("cplAfter")
        code = (r.get("verdict") or {}).get("code")
        icon = "🔴" if code == "degrade" else ("✅" if (b_ and a_ and a_ <= b_ * 1.15) else "⏳")
        amt = " $%.0f→$%.0f" % (ch["from"], ch["to"]) if ch.get("from") and ch.get("to") else ""
        chgd.append("%s %s (%s):%s %s · CPL %s → %s" % (
            icon, r["adset"], r["m"], amt, _dd(ch["date"]),
            ("$%.2f" % b_) if b_ is not None else "—",
            ("$%.2f" % a_) if a_ is not None else "—"))
    if chgd:
        L.append(""); L.append("📈 Після зміни бюджету (7 дн.):"); L += chgd

    # 🚀 можна масштабувати: вердикти пайплайну (та сама логіка, що на сайті);
    # без lifecycle (старий data.json) — стара локальна формула
    if LC:
        sc = ["• %s (%s): %s" % (r["adset"], r["m"], (r.get("verdict") or {}).get("text"))
              for r in LC if (r.get("verdict") or {}).get("code") in ("scale_ok", "holds")]
    else:
        d7map = {(a["m"], a["adset"]): a for a in (d.get("adsets", {}).get("d7") or [])}
        sc = []
        for s in (d.get("scaling") or []):
            if str(s["m"]).startswith("Инста"):
                continue
            since = (s.get("chg") or {}).get("date") or s.get("created")
            if not since or (today - datetime.date.fromisoformat(since)).days < 2:
                continue
            a = d7map.get((s["m"], s["adset"]))
            if not a or (a.get("book") or 0) < 2:
                continue
            if a.get("freq") is not None and a["freq"] >= 2.2:
                continue
            ck, cp = chk(s["m"]), a.get("cpa")
            if ck and cp is not None and cp >= ck * 0.15:
                continue
            b = s.get("budget") or 0
            sc.append("• %s (%s): $%.0f → $%.2f" % (s["adset"], s["m"], b, b * 1.25))
    if sc:
        L.append(""); L.append("🚀 Можна масштабувати сьогодні:"); L += sc

    # 📋 вечірній зріз ПО КОЖНОМУ активному ад-сету (прохання Ірини, 26.08):
    # бюджет, скільки днів живе, CPL за 3/7 днів (видно тренд), записи і CPA за
    # 7 днів, коротка дія з вердикту пайплайна
    LBL = {"degrade": "🔻 відкоти", "new_dead": "⛔ вимкни", "no_book": "⛔ не масштабуй",
           "cpa_high": "💰 здешеви запис", "cpa_warn": "💰 дорогий запис",
           "cpl_grow": "🎨 онови креатив", "freq": "🎨 свіжий креатив",
           "wait": "⏳ чекай", "new_test": "🆕 тест", "new_ok": "🆕 працює",
           "new_costly": "🆕 дорого", "holds": "🚀 можна +25%",
           "scale_ok": "🚀 можна +25%", "inst": "🚀 обережно +20%", "few_data": "⏳ мало даних"}
    per = []
    cur_m = None
    _f = lambda v: ("%.2f" % v) if v is not None else "—"
    for r in sorted(LC, key=lambda x: (str(x["m"]), -(x.get("budget") or 0))):
        if str(r["m"]).startswith("Инста"):
            continue
        if r["m"] != cur_m:
            cur_m = r["m"]
            per.append(""); per.append("%s:" % cur_m)
        per.append("• %s — $%.0f/д · %s дн · CPL %s/%s · зап %s · CPA %s · %s" % (
            r["adset"], r.get("budget") or 0, r.get("age") or "?",
            _f(r.get("cpl3")), _f(r.get("cpl7")),
            r["book7"] if r.get("book7") is not None else "—",
            ("$%.0f" % r["cpa7"]) if r.get("cpa7") is not None else "—",
            LBL.get((r.get("verdict") or {}).get("code"), "")))
    if per:
        L.append("")
        L.append("📋 Всі ад-сети (бюджет/день · вік · CPL 3д/7д · записи · CPA 7д · дія):")
        L += per

    L.append(""); L.append("📊 Дашборд: https://irynasyvashchenko.github.io/smas-dashboard/")
    return "\n".join(L)

def main():
    if not TOKEN:
        sys.exit("ERROR: TELEGRAM_BOT_TOKEN не заданий (додай секрет у GitHub Actions)")
    d = json.loads(_get(DATA_URL + "?cb=%d" % int(time.time())).decode("utf-8"))
    # Бриф триггериться після КОЖНОГО оновлення даних (workflow_run), але шлемо лише
    # після ВЕЧІРНЬОГО (коли дані за день готові) — або коли запущено вручну.
    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    upd = d.get("updated", "")
    hour = int(upd[11:13]) if len(upd) >= 13 else 0
    if not manual and hour < 21:
        print("skip: not evening yet (updated hour=%d)" % hour)
        return
    text = build(d)
    send(text)
    print("sent, %d chars" % len(text))

if __name__ == "__main__":
    main()

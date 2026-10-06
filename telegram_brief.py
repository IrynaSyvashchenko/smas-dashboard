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

    # 🔴 Алерти рахує пайплайн (build_alerts у refresh_dashboard.py) — той самий список, що
    # угорі дашборда. Правило Ірини (06.10): тільки те, що зараз втрачає гроші, з цифрами,
    # нормою міста, причиною й дією; якщо такого немає — блоку немає.
    al = []
    for a in (d.get("alerts") or []):
        if _hid(a.get("m")):
            continue
        al.append("🔴 " + a["text"])
        if a.get("why"):
            al.append("   причина: " + a["why"])
        for x in (a.get("adsets") or []):
            al.append("   • " + x)
        if a.get("note"):
            al.append("   ⚠ " + a["note"])
        al.append("   → " + a["act"])
    LC = [r for r in (d.get("lifecycle") or []) if not _hid(r.get("m"))]
    if al:
        L.append(""); L.append("Потребує уваги:"); L += al

    _dd = lambda s: "%s.%s" % (s[8:10], s[5:7]) if s and len(str(s)) >= 10 else "?"

    # 🆕 нові РК (≤3 днів): швидкий пульс — працює / дорого / рано судити
    news = []
    for r in LC:
        code = str((r.get("verdict") or {}).get("code") or "")
        if not code.startswith("new"):
            continue
        cpl = r.get("cpl3")
        news.append("• %s (%s): $%.0f/д · %d дн. · CPL %s" % (
            r["adset"], r["m"], r.get("budget") or 0, r.get("age") or 0,
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
        amt = " $%.0f→$%.0f" % (ch["from"], ch["to"]) if ch.get("from") and ch.get("to") else ""
        chgd.append("• %s (%s):%s %s · CPL %s → %s" % (
            r["adset"], r["m"], amt, _dd(ch["date"]),
            ("$%.2f" % b_) if b_ is not None else "—",
            ("$%.2f" % a_) if a_ is not None else "—"))
    if chgd:
        L.append(""); L.append("📈 Після зміни бюджету (7 дн.):"); L += chgd

    # 📋 вечірній зріз ПО КОЖНОМУ активному ад-сету (прохання Ірини, 26.08):
    # бюджет, скільки днів живе, CPL за 3/7 днів (видно тренд), записи і CPA за 7 днів.
    # Дії-вердикти за CPL прибрано (06.10): рекомендації — лише в алертах вище.
    per = []
    cur_m = None
    _f = lambda v: ("%.2f" % v) if v is not None else "—"
    for r in sorted(LC, key=lambda x: (str(x["m"]), -(x.get("budget") or 0))):
        if str(r["m"]).startswith("Инста"):
            continue
        if r["m"] != cur_m:
            cur_m = r["m"]
            per.append(""); per.append("%s:" % cur_m)
        per.append("• %s — $%.0f/д · %s дн · CPL %s/%s · зап %s · CPA %s" % (
            r["adset"], r.get("budget") or 0, r.get("age") or "?",
            _f(r.get("cpl3")), _f(r.get("cpl7")),
            r["book7"] if r.get("book7") is not None else "—",
            ("$%.0f" % r["cpa7"]) if r.get("cpa7") is not None else "—"))
    if per:
        L.append("")
        L.append("📋 Всі ад-сети (бюджет/день · вік · CPL 3д/7д · записи · CPA 7д):")
        L += per

    L.append(""); L.append("📊 Дашборд: https://irynasyvashchenko.github.io/smas-dashboard/")
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

def main():
    if not TOKEN:
        sys.exit("ERROR: TELEGRAM_BOT_TOKEN не заданий (додай секрет у GitHub Actions)")
    d = json.loads(_get(DATA_URL + "?cb=%d" % int(time.time())).decode("utf-8"))
    # Бриф триггериться після КОЖНОГО оновлення даних (workflow_run), але шлемо лише
    # після ВЕЧІРНЬОГО (коли дані за день готові) — або коли запущено вручну.
    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    upd = d.get("updated", "")
    hour = int(upd[11:13]) if len(upd) >= 13 else 0
    # Ірина хоче бриф ДО 23:00 (05.09). Планувальник GitHub возить крони з лагом
    # 0–2.5 год, тому у refresh.yml ДВА вечірні слоти (18:25Z і 20:25Z), а шлемо
    # ЛИШЕ коли дані приїхали у вікно 21..22 за Прагою — за будь-якого лагу
    # кваліфікується рівно один запуск (без дублів), бриф приходить ~21:30–23:00.
    if not manual and hour not in (21, 22):
        print("skip: outside evening window 21..22 (updated hour=%d)" % hour)
        return
    text = token_warn(d) + build(d)
    send(text)
    print("sent, %d chars" % len(text))

if __name__ == "__main__":
    main()

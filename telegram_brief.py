#!/usr/bin/env python3
"""Вечірній Telegram-бриф по Meta-рекламі «SMAS-ліфтинг».

Запускається з GitHub Actions о 22:30 за Прагою (20:30 UTC) — у хмарі, незалежно
від того, чи ввімкнений комп'ютер Ірини. Читає готовий data.json з GitHub Pages,
збирає короткий звіт за ОСТАННІЙ ЗАКРИТИЙ день і шле в Telegram через Bot API.

Дані вже пораховані дашбордом — тут нічого не тягнеться з Meta.
Секрет (GitHub Actions): TELEGRAM_BOT_TOKEN — токен бота @ad_smas_lifting_bot.
"""
import os, json, time, datetime, urllib.request, urllib.parse, sys

CHAT_ID  = "901823018"
TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DATA_URL = "https://irynasyvashchenko.github.io/smas-dashboard/data.json"

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "smas-brief", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=90).read()

def send(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text,
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
    rows = []; tsp = tld = tbk = 0
    for m, n in M.items():
        if n.get("act") is False:
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
        if sp <= 0 and ld <= 0:
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
    for a in (d.get("adsets", {}).get("d7") or []):
        m = a["m"]
        if str(m).startswith("Инста") or a.get("act") is False or a["adset"] in dead:
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
    L.append("")
    if al:
        L.append("🔴 Потребує уваги:"); L += al
    else:
        L.append("✅ Все спокійно: гроші без лідів не горять, записи не задорогі.")

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

    L.append(""); L.append("📊 Дашборд: https://irynasyvashchenko.github.io/smas-dashboard/")
    return "\n".join(L)

def main():
    if not TOKEN:
        sys.exit("ERROR: TELEGRAM_BOT_TOKEN не заданий (додай секрет у GitHub Actions)")
    d = json.loads(_get(DATA_URL + "?cb=%d" % int(time.time())).decode("utf-8"))
    text = build(d)
    send(text)
    print("sent, %d chars" % len(text))

if __name__ == "__main__":
    main()

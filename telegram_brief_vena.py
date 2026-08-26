#!/usr/bin/env python3
"""Ранковий Telegram-бриф по кабінету ad5: Стомат Київ + Стомат Квіз, СМАС Відень/Київ/Тернопіль.

Запускається з GitHub Actions після кожного «Refresh vena dashboard (ad5)»
(workflow_run), але шле ЛИШЕ після першого ранкового оновлення (година updated
7..8 за Києвом) — кабінет живе за Києвом (UTC+3), тож на цей момент вчорашній
день повністю закритий. Ручний запуск (workflow_dispatch) шле завжди.

Дані вже пораховані дашбордом (vena/data.json) — тут нічого не тягнеться з Meta.
Секрет (GitHub Actions): TELEGRAM_BOT_TOKEN — токен бота @ad_smas_lifting_bot.
"""
import os, json, time, datetime, urllib.request, urllib.parse, sys

CHAT_ID  = "901823018"
TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DATA_URL = "https://raw.githubusercontent.com/IrynaSyvashchenko/smas-dashboard/main/vena/data.json"
DASH_URL = "https://irynasyvashchenko.github.io/smas-dashboard/vena/"

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vena-brief", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=90).read()

def send(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text,
                                   "disable_web_page_preview": "true"}).encode("utf-8")
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30).read()

def build(d):
    upd = d["updated"]
    try:
        ut = datetime.datetime.fromisoformat(upd)
        if (datetime.datetime.now(datetime.timezone.utc) - ut).total_seconds() > 15 * 3600:
            return "⚠️ Дашборд ad5 не оновлюється з %s, перевір GitHub Actions" % upd
    except Exception:
        pass

    # Звітний день = вчора за Києвом: усі yest-зрізи в data.json рахуються від дати updated.
    today = datetime.date.fromisoformat(upd[:10])
    rday  = today - datetime.timedelta(days=1)
    rs    = rday.isoformat()

    M = d["managers"]
    rows = []; tsp = tld = tbk = 0
    for m, n in M.items():
        try:
            i = n["dates"].index(rs)
        except (KeyError, ValueError):
            continue
        sp = n["spend"][i] or 0; ld = n["leads"][i] or 0
        bk = n.get("bookingsYest") or 0
        if sp <= 0 and ld <= 0 and not bk:
            continue
        tsp += sp; tld += ld; tbk += bk
        rows.append((m, sp, ld, bk))
    rows.sort(key=lambda r: -r[1])

    cpa = ("$%.2f" % (tsp / tbk)) if tbk else "—"
    L = ["🦷 Кабінет ad5 за вчора (%s)" % rday.strftime("%d.%m"),
         "Разом: $%.0f · %d лідів · %d записів · CPA %s" % (tsp, tld, tbk, cpa), ""]
    for m, sp, ld, bk in rows:
        cpl = (" (лід $%.2f)" % (sp / ld)) if ld else ""
        cpb = (" (запис $%.2f)" % (sp / bk)) if bk else ""
        L.append("%s: $%.0f · %d л%s · %d з%s" % (m, sp, ld, cpl, bk, cpb))

    def days_live(m):
        s = M.get(m, {}).get("start")
        return ((today - datetime.date.fromisoformat(s)).days + 1) if s else 99

    al, dead = [], set()
    for a in (d.get("adsets", {}).get("yest") or []):
        if a.get("act") is False:
            continue
        if (a.get("spend") or 0) >= 5 and (a.get("leads") or 0) == 0:
            dead.add(a["adset"])
            al.append("• %s (%s): $%.2f без лідів — глянь або вимкни" % (a["adset"], a["m"], a["spend"]))
    for a in (d.get("adsets", {}).get("d7") or []):
        m = a["m"]
        if a.get("act") is False or a["adset"] in dead or days_live(m) < 4:
            continue
        dl = days_live(m)
        per = ("%d дн." % dl) if dl < 7 else "7 днів"
        if (a.get("spend") or 0) >= 25 and (a.get("book") or 0) == 0:
            al.append("• %s (%s): $%.0f за %s і 0 записів" % (a["adset"], m, a["spend"], per))
        elif a.get("cpa") is not None and a["cpa"] >= 35:
            al.append("• %s (%s): запис $%.2f (%s) — дорого" % (a["adset"], m, a["cpa"], per))
    # креатив, що за 7 днів з'їв $15+ і не дав жодного запису (ловить слабкі відео
    # всередині робочого ад-сета, як quiz_video1)
    for c in (d.get("creatives", {}).get("d7") or []):
        if c.get("act") is False or c.get("adset") in dead:
            continue
        if (c.get("spend") or 0) >= 15 and (c.get("book") or 0) == 0:
            al.append("• Креатив %s (%s): $%.2f за 7 днів без записів — кандидат на вимкнення"
                      % (c["name"], c["m"], c["spend"]))
    for c in (d.get("creatives", {}).get("yest") or []):
        if c.get("act") is False or c.get("adset") in dead:
            continue
        if c.get("freq") is not None and c["freq"] >= 2.5:
            al.append("• Креатив %s (%s): частота %.2f — вигорів" % (c["name"], c["m"], c["freq"]))
    L.append("")
    if al:
        L.append("🔴 Що змінити:"); L += al
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
        if a.get("cpa") is not None and a["cpa"] >= 35:
            continue
        b = s.get("budget") or 0
        sc.append("• %s (%s): $%.0f → $%.2f" % (s["adset"], s["m"], b, b * 1.25))
    if sc:
        L.append(""); L.append("🚀 Можна масштабувати сьогодні:"); L += sc

    L.append(""); L.append("📊 Дашборд: %s" % DASH_URL)
    return "\n".join(L)

def main():
    if not TOKEN:
        sys.exit("ERROR: TELEGRAM_BOT_TOKEN не заданий (додай секрет у GitHub Actions)")
    d = json.loads(_get(DATA_URL + "?cb=%d" % int(time.time())).decode("utf-8"))
    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    upd = d.get("updated", "")
    hour = int(upd[11:13]) if len(upd) >= 13 else -1
    # шлемо після першого ранкового оновлення (~07:40 за Києвом); решту запусків пропускаємо
    if not manual and not (7 <= hour <= 8):
        print("skip: not the morning refresh (updated hour=%d)" % hour)
        return
    text = build(d)
    send(text)
    print("sent, %d chars" % len(text))

if __name__ == "__main__":
    main()

"""
Chhattisgarh Govt Job Alerts
Roz Google News se Chhattisgarh ki nayi sarkari vacancies dhoondhta hai
aur Email + WhatsApp (CallMeBot) par bhejta hai.
"""
import os, json, smtplib, html, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

SEEN_FILE = "seen.json"
MAX_AGE_DAYS = 3          # sirf pichhle 3 din ki khabrein
MAX_ITEMS = 30            # ek email mein zyada se zyada itni vacancies

# (search query, language)
QUERIES = [
    ("Chhattisgarh recruitment vacancy", "en"),
    ("Chhattisgarh govt jobs notification", "en"),
    ("CGPSC recruitment", "en"),
    ("CG Vyapam recruitment", "en"),
    ("CG Police bharti", "en"),
    ("छत्तीसगढ़ भर्ती", "hi"),
    ("छत्तीसगढ़ सरकारी नौकरी", "hi"),
    ("व्यापम भर्ती विज्ञापन", "hi"),
]

JOB_WORDS = ["recruit", "vacanc", "bharti", "bharati", "notification", "posts",
             "job", "naukri", "apply", "exam", "cgpsc", "vyapam",
             "भर्ती", "पद", "नौकरी", "रिक्त", "आवेदन", "विज्ञापन", "व्यापम", "परीक्षा"]
CG_WORDS = ["chhattisgarh", "chattisgarh", "cgpsc", "vyapam", "cg ", "raipur",
            "bilaspur", "durg", "korba", "bastar", "छत्तीसगढ़", "सीजी", "रायपुर",
            "बिलासपुर", "दुर्ग", "व्यापम"]


def fetch_feed(query, lang):
    q = urllib.parse.quote(f"{query} when:{MAX_AGE_DAYS}d")
    if lang == "hi":
        url = f"https://news.google.com/rss/search?q={q}&hl=hi&gl=IN&ceid=IN:hi"
    else:
        url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        source = (it.findtext("source") or "").strip()
        pub = it.findtext("pubDate")
        try:
            date = parsedate_to_datetime(pub) if pub else None
        except Exception:
            date = None
        items.append({"title": title, "link": link, "source": source, "date": date})
    return items


def is_relevant(title):
    t = title.lower() + " "
    return any(w in t for w in JOB_WORDS) and any(w in t for w in CG_WORDS)


def key_of(title):
    # Google News title = "Headline - Source"; headline se dedupe
    return title.rsplit(" - ", 1)[0].strip().lower()


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen[-2000:], f, ensure_ascii=False, indent=0)


def send_email(jobs):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("TO_EMAIL", user)
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y")

    rows = ""
    for j in jobs:
        d = j["date"].astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b") if j["date"] else ""
        rows += (f'<li style="margin-bottom:10px"><a href="{html.escape(j["link"])}">'
                 f'{html.escape(j["title"])}</a><br><small style="color:#666">{d}</small></li>')
    body = f"""<html><body style="font-family:Arial,sans-serif">
<h2>Chhattisgarh Govt Vacancies – {today}</h2>
<p>{len(jobs)} nayi updates mili hain:</p><ol>{rows}</ol>
<hr><p style="font-size:13px">Apply karne se pehle official website par zaroor confirm karein:<br>
CGPSC: https://psc.cg.gov.in<br>CG Vyapam: https://vyapam.cgstate.gov.in<br>
Bharti portal: https://cg.gov.in</p></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"CG Govt Jobs Alert: {len(jobs)} nayi vacancies ({today})"
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    print("Email bhej diya:", to)


def send_whatsapp(jobs):
    phone = os.environ.get("WHATSAPP_PHONE")
    key = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not key:
        print("WhatsApp secrets nahi mile, WhatsApp skip.")
        return
    lines = [f"*CG Govt Jobs Alert* ({len(jobs)} nayi)"]
    for i, j in enumerate(jobs[:10], 1):
        lines.append(f"{i}. {j['title'].rsplit(' - ', 1)[0]}")
    if len(jobs) > 10:
        lines.append(f"...aur {len(jobs) - 10} more")
    lines.append("Poori list aur links email mein hain.")
    text = "\n".join(lines)[:1500]
    url = ("https://api.callmebot.com/whatsapp.php?phone=" + urllib.parse.quote(phone)
           + "&text=" + urllib.parse.quote(text) + "&apikey=" + urllib.parse.quote(key))
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            print("WhatsApp status:", r.status)
    except Exception as e:
        print("WhatsApp error (email phir bhi gaya):", e)


def main():
    seen = load_seen()
    seen_set = set(seen)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    new_jobs, batch_keys = [], set()

    for query, lang in QUERIES:
        try:
            items = fetch_feed(query, lang)
        except Exception as e:
            print("Feed error:", query, e)
            continue
        for it in items:
            k = key_of(it["title"])
            if not k or k in seen_set or k in batch_keys:
                continue
            if it["date"] and it["date"] < cutoff:
                continue
            if not is_relevant(it["title"]):
                continue
            batch_keys.add(k)
            new_jobs.append(it)

    new_jobs.sort(key=lambda j: j["date"] or cutoff, reverse=True)
    new_jobs = new_jobs[:MAX_ITEMS]

    if not new_jobs:
        print("Aaj koi nayi vacancy nahi mili.")
        return

    send_email(new_jobs)
    send_whatsapp(new_jobs)
    seen.extend(key_of(j["title"]) for j in new_jobs)
    save_seen(seen)


if __name__ == "__main__":
    main()

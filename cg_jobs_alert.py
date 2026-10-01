"""
CG Govt Job Alert
-----------------
- Google News + Chhattisgarh ki official websites (zila, court, Vyapam, CGPSC, CHiPS...)
  se aapke fields ki vacancies dhoondhta hai.
- Roz chalta hai: bade pad (Patwari, Sachiv, Data Entry...) ki nayi vacancy turant email karta hai.
- Hafte mein ek baar (Somvar) poori list category-wise bhejta hai, saath mein
  "Last date jaldi" wali vacancies ka reminder bhi.
Saari settings config.yaml mein hain.
"""
import os, re, json, hashlib, smtplib, html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin
from concurrent.futures import ThreadPoolExecutor

import requests, urllib3, yaml
from bs4 import BeautifulSoup

urllib3.disable_warnings()
IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime.now(IST).date()
STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"}

CFG = yaml.safe_load(open("config.yaml", encoding="utf-8"))


# ---------------------------------------------------------------- matching
def build_matcher(words):
    plain, pats = [], []
    for w in words or []:
        w = str(w).strip().lower()
        if not w:
            continue
        if w.isascii() and len(w) <= 4:          # chhote shabd poore word ke roop mein
            pats.append(re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])"))
        else:
            plain.append(w)
    def match(text):
        return any(p in text for p in plain) or any(p.search(text) for p in pats)
    return match

M_EXCLUDE = build_matcher(CFG["exclude"])
M_STATUS = build_matcher(CFG["status_words"])
M_JOB = build_matcher(CFG["job_words"])
M_CG = build_matcher(CFG["cg_words"])
M_NATIONAL = build_matcher(CFG["national_ok"])
M_URGENT = build_matcher(CFG["urgent_keywords"])
CATS = [(name, build_matcher(words)) for name, words in CFG["categories"].items()]
CAT_OTHER = "📌 Anya CG Govt Vacancies (PDF kholkar pad check karein)"
CAT_STATUS = "📋 Result / Merit List / Admit Card (aapke fields)"
CAT_REMIND = "⏰ Last Date Jaldi - abhi apply karein"


def classify(item):
    """Item ko category deta hai, ya None (nahi chahiye)."""
    t = " " + item["title"].lower() + " "
    if M_EXCLUDE(t):
        return None
    cat = next((name for name, m in CATS if m(t)), None)
    status = M_STATUS(t)
    if item["kind"] == "news":
        if not cat or not (M_JOB(t) or status):
            return None
        if not (M_CG(t) or M_NATIONAL(t)):
            return None
    else:  # official site
        if not cat:
            if CFG.get("include_generic_official") and M_JOB(t) and not status:
                return CAT_OTHER
            return None
    return CAT_STATUS if status else cat


# ---------------------------------------------------------------- dates
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
RE_NUM = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b")
RE_TXT = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]+([A-Za-z]{3,9})[,\s-]+(20\d{2})\b")


def find_dates(text):
    found = []
    for m in RE_NUM.finditer(text):
        try:
            found.append((m.start(), date(int(m.group(3)), int(m.group(2)), int(m.group(1)))))
        except ValueError:
            pass
    for m in RE_TXT.finditer(text):
        mon = MONTHS.get(m.group(2)[:3].lower())
        if mon:
            try:
                found.append((m.start(), date(int(m.group(3)), mon, int(m.group(1)))))
            except ValueError:
                pass
    return [d for _, d in sorted(found) if 2020 <= d.year <= 2035]


# ---------------------------------------------------------------- fetching
def get(url):
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
            if r.status_code == 200 and r.content:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = type(e).__name__
    raise RuntimeError(last)


def fetch_news(query):
    days = CFG["lookback_days"]
    hindi = not query.isascii()
    q = quote(f"{query} when:{days}d")
    url = (f"https://news.google.com/rss/search?q={q}&hl=hi&gl=IN&ceid=IN:hi" if hindi else
           f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en")
    root = ET.fromstring(get(url).content)
    cutoff = TODAY - timedelta(days=days)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "News").strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(IST).date()
        except Exception:
            pub = None
        if pub and pub < cutoff:
            continue
        headline = title.rsplit(" - ", 1)[0].strip()
        items.append({"kind": "news", "title": headline, "link": (it.findtext("link") or "").strip(),
                      "source": src, "start": pub, "end": None,
                      "key": "news|" + re.sub(r"\W+", " ", headline.lower()).strip()})
    return items


def parse_page(content, base_url, source):
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    items, used = [], set()

    def add(title, link, text):
        title = re.sub(r"\s+", " ", title).strip()[:300]
        if len(title) < 10:
            return
        dates = find_dates(text)
        start = dates[0] if dates else None
        end = dates[-1] if len(dates) >= 2 else None
        key = "site|" + hashlib.md5(f"{source}|{link}|{title.lower()[:120]}".encode()).hexdigest()
        if key in used:
            return
        used.add(key)
        items.append({"kind": "site", "title": title, "link": link, "source": source,
                      "start": start, "end": end, "key": key})

    for tr in soup.find_all("tr"):           # tables (zila / court sites)
        tds = tr.find_all("td")
        if not tds:
            continue
        texts = [td.get_text(" ", strip=True) for td in tds]
        title = texts[0] if len(texts[0]) >= 10 else max(texts, key=len)
        a = tr.find("a", href=True)
        link = urljoin(base_url, a["href"]) if a else base_url
        add(title, link, tr.get_text(" ", strip=True))

    for a in soup.find_all("a", href=True):  # normal links (Vyapam, CGPSC, etc.)
        if a.find_parent("tr"):
            continue
        txt = a.get_text(" ", strip=True)
        if len(txt) < 15:
            continue
        ctx = a.parent.get_text(" ", strip=True)[:400] if a.parent else txt
        add(txt, urljoin(base_url, a["href"]), ctx)
    return items


def site_list():
    out = []
    for d in CFG.get("district_sites") or []:
        out.append((f"Zila {d.split('.')[0].title()}", f"https://{d}/en/notice_category/recruitment/"))
    for d in CFG.get("court_sites") or []:
        name = f"Court {d.split('.')[0].title()}"
        out.append((name, f"https://{d}/notice-category/recruitments/"))
        out.append((name, f"https://{d}/document-category/recruitment/"))
    for s in CFG.get("other_sites") or []:
        out.append((s["name"], s["url"]))
    return out


def fetch_site(src_url):
    name, url = src_url
    return parse_page(get(url).content, url, name)


# ---------------------------------------------------------------- state
def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("items", {})
    st.setdefault("baselined", [])
    return st


def save_state(st):
    # purani entries ko chhota kar do (par key yaad rakho taaki dobara "nayi" na lage)
    for k, v in list(st["items"].items()):
        first = date.fromisoformat(v["first_seen"])
        end = date.fromisoformat(v["end"]) if v.get("end") else None
        if "cat" in v and (TODAY - first).days > 150 and not (end and end >= TODAY):
            st["items"][k] = {"first_seen": "2000-01-01", "end": v.get("end")}
        if k.startswith("news|") and (TODAY - first).days > 60:
            del st["items"][k]
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def iso(d):
    return d.isoformat() if d else None


# ---------------------------------------------------------------- email
def fmt(d):
    return d.strftime("%d/%m/%Y") if d else ""


def render(groups, title, intro, failed):
    order = [CAT_REMIND] + [n for n, _ in CATS] + [CAT_OTHER, CAT_STATUS]
    parts = [f'<div style="font-family:Arial,sans-serif;max-width:720px;margin:auto">'
             f'<h2 style="color:#1a4b8c">{html.escape(title)}</h2><p>{intro}</p>']
    for cat in order:
        rows = groups.get(cat)
        if not rows:
            continue
        parts.append(f'<h3 style="background:#eef3fb;padding:6px 10px;border-radius:6px">'
                     f'{html.escape(cat)} ({len(rows)})</h3><ol style="padding-left:22px">')
        for it in rows:
            meta = [html.escape(it["source"])]
            start = date.fromisoformat(it["start"]) if it.get("start") else None
            end = date.fromisoformat(it["end"]) if it.get("end") else None
            if start:
                meta.append(("Start " if end else "Date ") + fmt(start))
            if end:
                left = (end - TODAY).days
                color = "#c0392b" if left <= 7 else "#555"
                meta.append(f'<b style="color:{color}">Last date {fmt(end)} ({left} din baaki)</b>')
            badge = ' <span style="background:#e74c3c;color:#fff;font-size:11px;padding:1px 5px;border-radius:4px">IMPORTANT</span>' if it.get("urgent") else ""
            tag = "Official site" if it["kind"] == "site" else "News"
            parts.append(f'<li style="margin-bottom:10px"><a href="{html.escape(it["link"])}">'
                         f'{html.escape(it["title"])}</a>{badge}<br>'
                         f'<small style="color:#555">{tag} • {" • ".join(meta)}</small></li>')
        parts.append("</ol>")
    parts.append('<hr><p style="font-size:13px;color:#444">Apply karne se pehle official notification zaroor padhein. '
                 'Main websites: <a href="https://vyapamcg.cgstate.gov.in">CG Vyapam</a> • '
                 '<a href="https://psc.cg.gov.in">CGPSC</a> • <a href="https://highcourt.cg.gov.in">High Court</a> • '
                 '<a href="https://indiapostgdsonline.gov.in">India Post GDS</a></p>')
    if failed:
        parts.append('<p style="font-size:11px;color:#999">Ye sites is baar nahi khuli (sarkari site down/block ho sakti hai): '
                     + html.escape(", ".join(sorted(set(failed)))) + '</p>')
    parts.append("</div>")
    return "".join(parts)


def send_email(subject, body):
    if os.environ.get("DRY_RUN"):
        open("preview.html", "w", encoding="utf-8").write(body)
        print("DRY_RUN: preview.html likh diya |", subject)
        return
    user, pwd = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("TO_EMAIL") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
    print("Email bheja:", subject)


# ---------------------------------------------------------------- main
def main():
    mode = os.environ.get("MODE", "auto").strip() or "auto"
    if mode == "auto":
        mode = "weekly" if datetime.now(IST).weekday() == int(CFG.get("weekly_day", 0)) else "daily"
    print("Mode:", mode)

    st = load_state()
    failed, collected = [], []

    with ThreadPoolExecutor(max_workers=12) as ex:
        news_jobs = {ex.submit(fetch_news, q): q for q in CFG["news_queries"]}
        site_jobs = {ex.submit(fetch_site, s): s for s in site_list()}
        for f, q in news_jobs.items():
            try:
                collected += f.result()
            except Exception as e:
                print("News fail:", q, e)
        site_ok = set()
        for f, (name, url) in site_jobs.items():
            try:
                its = f.result()
                for it in its:
                    it["_src_url"] = url
                collected += its
                site_ok.add(url)
            except Exception as e:
                failed.append(name)
                print("Site fail:", name, url, e)

    new_urgent = []
    lookback = TODAY - timedelta(days=CFG["lookback_days"])
    for it in collected:
        cat = classify(it)
        if not cat or it["key"] in st["items"]:
            continue
        if it["end"] and it["end"] < TODAY:                    # last date nikal gayi
            continue
        if it["kind"] == "site":
            baseline = it["_src_url"] not in st["baselined"]
            fresh = (it["end"] and it["end"] >= TODAY) or (it["start"] and it["start"] >= lookback)
            if baseline and not fresh:                         # pehli baar: purani cheezein chupchap yaad rakho
                st["items"][it["key"]] = {"first_seen": "2000-01-01", "end": iso(it["end"])}
                continue
            if it["start"] and it["start"] < lookback and not (it["end"] and it["end"] >= TODAY):
                continue
        rec = {"title": it["title"], "link": it["link"], "source": it["source"], "kind": it["kind"],
               "cat": cat, "start": iso(it["start"]), "end": iso(it["end"]),
               "first_seen": TODAY.isoformat(), "urgent": bool(M_URGENT(it["title"].lower())) and cat != CAT_STATUS}
        st["items"][it["key"]] = rec
        if rec["urgent"]:
            new_urgent.append(rec)

    for url in {it["_src_url"] for it in collected if it["kind"] == "site"}:
        if url not in st["baselined"]:
            st["baselined"].append(url)

    if mode == "daily":
        if new_urgent:
            groups = {}
            for r in new_urgent:
                groups.setdefault(r["cat"], []).append(r)
            body = render(groups, "🔔 Important Vacancy Alert",
                          f"{len(new_urgent)} important nayi vacancy mili hai. Poori list Somvar ko aayegi.", [])
            send_email(f"🔔 Important: {new_urgent[0]['title'][:70]}" +
                       (f" +{len(new_urgent)-1} more" if len(new_urgent) > 1 else ""), body)
        else:
            print("Aaj koi important nayi vacancy nahi.")
        save_state(st)
        return

    # weekly
    groups, count = {}, 0
    week_start = TODAY - timedelta(days=CFG["lookback_days"])
    for k, r in st["items"].items():
        if "cat" not in r:
            continue
        first = date.fromisoformat(r["first_seen"])
        end = date.fromisoformat(r["end"]) if r.get("end") else None
        if first > week_start:
            groups.setdefault(r["cat"], []).append(r)
            count += 1
        elif end and TODAY <= end <= TODAY + timedelta(days=CFG["reminder_days"]) and r["cat"] != CAT_STATUS:
            groups.setdefault(CAT_REMIND, []).append(r)
    for cat in groups:
        groups[cat].sort(key=lambda r: (not r.get("urgent"), r.get("end") or "9999", r["title"]))
    reminders = len(groups.get(CAT_REMIND, []))
    intro = (f"Is hafte <b>{count}</b> nayi updates mili hain"
             + (f" aur <b>{reminders}</b> vacancies ki last date paas hai" if reminders else "") + "."
             if (count or reminders) else "Is hafte aapke fields mein koi nayi vacancy nahi mili. System chal raha hai 👍")
    body = render(groups, f"Weekly CG Govt Job Alert – {TODAY.strftime('%d %b %Y')}", intro, failed)
    send_email(f"📋 Weekly CG Govt Jobs: {count} nayi" + (f", {reminders} last date paas" if reminders else ""), body)
    save_state(st)


if __name__ == "__main__":
    main()

"""Email alerts. Two free delivery paths, chosen by which secrets exist:

  Gmail SMTP (default): GMAIL_USER + GMAIL_APP_PASSWORD + ALERT_TO
      Uses an app password (Google Account -> Security -> App passwords).
  Resend:               RESEND_API_KEY + ALERT_TO (free tier, 100/day)

Only called for finalists, so volume is a few emails a week at most.
"""
from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from email.mime.text import MIMEText


def _html_report(item: dict) -> str:
    d = item["domain"]
    h = item.get("history") or {}
    snaps = item.get("snapshots") or []
    rows = "".join(
        f"<tr><td style='padding:2px 10px'>{s['year']}</td>"
        f"<td style='padding:2px 10px'>{(s.get('title') or ('(fetch failed)' if not s.get('ok') else '(no title)'))}</td></tr>"
        for s in snaps)
    dr = item.get("ahrefs_dr")
    opr = item.get("open_pagerank")
    moz = item.get("moz_da")
    gidx = item.get("google_indexed")
    metrics = []
    if dr is not None:
        metrics.append(f"Domain Rating by Ahrefs: <b>{dr}</b>")
    if opr is not None:
        metrics.append(f"Open Page Rank: <b>{opr}</b>")
    if item.get("ref_domains"):
        metrics.append(f"Referring domains: <b>{item['ref_domains']:,}</b>")
    if moz is not None:
        metrics.append(f"Moz DA: <b>{moz}</b>")
    if gidx is not None:
        metrics.append(f"Google indexed pages (site:): <b>{gidx}</b>")
    ahrefs_base = item.get("_ahrefs_link_base") or "https://app.ahrefs.com"
    links = (
        f"<a href='https://web.archive.org/web/*/{d}'>Wayback timeline</a> &middot; "
        f"<a href='{ahrefs_base}/site-explorer/overview?target={d}&mode=subdomains'>Ahrefs overview</a> &middot; "
        f"<a href='https://www.spaceship.com/domain-search/?query={d}'>Register</a> &middot; "
        f"<a href='https://rdap.org/domain/{d}'>RDAP status</a>"
    )
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;max-width:640px">
      <h2 style="margin:0 0 4px">{d}</h2>
      <p style="margin:0 0 10px;color:#444">
        Score <b>{item.get('score')}</b> / 100 &middot;
        Status: <b>{item.get('status','')}</b>
        {('&middot; drops ' + item['drop_date']) if item.get('drop_date') else ''}
      </p>
      <p style="margin:0 0 10px">{' &middot; '.join(metrics) or 'No external metrics configured'}</p>
      <p style="margin:0 0 6px;color:#444">
        First archived <b>{h.get('first_year','?')}</b> &middot;
        {h.get('months_captured','?')} captured months &middot;
        coverage {h.get('coverage','?')} &middot;
        title consistency {item.get('consistency','?')}
      </p>
      <table style="border-collapse:collapse;background:#f5f6f8;border-radius:6px">{rows}</table>
      <p style="margin:12px 0 0">{links}</p>
      <p style="margin:12px 0 0;color:#888;font-size:12px">Domain Rating by Ahrefs (ahrefs.com).
      Verify backlinks and anchors manually before registering.</p>
    </div>"""


def send_alert(item: dict, cfg: dict) -> bool:
    to = os.environ.get("ALERT_TO")
    if not to:
        # stdout may be public; never print the domain here
        print("ALERT_TO not set; alert suppressed (details in the data repo)")
        return False
    subject = (f"{cfg['alerts'].get('subject_prefix','[Domain Finder]')} "
               f"{item['domain']} scored {item.get('score')} "
               f"({item.get('status','')})")
    body = _html_report(item)

    if os.environ.get("RESEND_API_KEY"):
        payload = json.dumps({
            "from": "Domain Finder <onboarding@resend.dev>",
            "to": [to], "subject": subject, "html": body,
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload,
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                     "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20)
            return True
        except Exception as e:
            print("resend failed:", e)

    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if user and pw:
        msg = MIMEText(body, "html", "utf-8")
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(user, pw)
                s.sendmail(user, [to], msg.as_string())
            return True
        except Exception as e:
            print("gmail failed:", e)
    return False

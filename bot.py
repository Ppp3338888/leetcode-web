"""
Bot logic — runs one thread per user.
Each user has their own LeetCode cookies + Gmail token.
"""

import time, base64, threading, requests
from datetime import datetime
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

LEETCODE_GRAPHQL = "https://leetcode.com/graphql"

UPCOMING_Q = """
query {
  allContests {
    title
    titleSlug
    startTime
    duration
    isVirtual
  }
}"""

REGISTER_M = """
mutation contestRegister($titleSlug: String!) {
  contestRegister(contestSlug: $titleSlug) { ok }
}"""

UNREGISTER_M = """
mutation contestUnregister($titleSlug: String!) {
  contestUnregister(contestSlug: $titleSlug) { ok }
}"""

CHECK_REG_Q = """
query contestDetailPage($contestSlug: String!) {
  contestDetailPage(contestSlug: $contestSlug) { isRegistered }
}"""

# user_id → thread + stop event
_threads = {}
_stop_events = {}

# per-user state
_state = {}  # user_id → {slug: "IN"/"DROP"/"SAFE"/None}
_registered = {}   # user_id → set of slugs
_12h_sent = {}     # user_id → set of slugs
_5min_sent = {}    # user_id → set of slugs

# ─── LEETCODE ─────────────────────────────────────────────────────────────────

def lc_headers(session, csrf):
    return {
        "Content-Type": "application/json",
        "Cookie": f"LEETCODE_SESSION={session}; csrftoken={csrf}",
        "x-csrftoken": csrf,
        "Origin": "https://leetcode.com",
        "Referer": "https://leetcode.com/contest/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

def lc_post(session, csrf, query, variables=None):
    r = requests.post(
        LEETCODE_GRAPHQL,
        json={"query": query, "variables": variables or {}},
        headers=lc_headers(session, csrf),
        timeout=10,
    )
    print(f"[LC DEBUG] status={r.status_code} body={r.text[:500]}")
    r.raise_for_status()
    return r.json()

def get_upcoming(session, csrf):
    data = lc_post(session, csrf, UPCOMING_Q)
    now_ts = time.time()
    return [
        c for c in data.get("data", {}).get("allContests", [])
        if now_ts < c["startTime"] < now_ts + 49 * 3600
        and not c.get("isVirtual", False)
    ]

def is_registered(session, csrf, slug):
    data = lc_post(session, csrf, CHECK_REG_Q, {"contestSlug": slug})
    return data.get("data", {}).get("contestDetailPage", {}).get("isRegistered", False)

def register(session, csrf, slug):
    data = lc_post(session, csrf, REGISTER_M, {"titleSlug": slug})
    r = data.get("data", {}).get("contestRegister", {})
    return r.get("ok", False), r.get("error", "")

def unregister(session, csrf, slug):
    data = lc_post(session, csrf, UNREGISTER_M, {"titleSlug": slug})
    r = data.get("data", {}).get("contestUnregister", {})
    return r.get("ok", False), r.get("error", "")

# ─── GMAIL ────────────────────────────────────────────────────────────────────

def get_gmail(token_json):
    creds = Credentials.from_authorized_user_info(
        __import__("json").loads(token_json),
        scopes=["https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.readonly"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)

def send_email(gmail, to, subject, body):
    msg = MIMEText(body)
    msg["to"] = to
    msg["from"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

def check_reply(gmail, since, keywords):
    result = gmail.users().messages().list(
        userId="me", q=f"in:inbox after:{int(since)}"
    ).execute()
    for msg in result.get("messages", []):
        data = gmail.users().messages().get(userId="me", id=msg["id"]).execute()
        if int(data["internalDate"]) / 1000 < since:
            continue
        payload = data.get("payload", {})
        body = ""
        if "parts" in payload:
            for p in payload["parts"]:
                if p.get("mimeType") == "text/plain":
                    body = base64.urlsafe_b64decode(
                        p["body"].get("data", "")
                    ).decode("utf-8", errors="ignore")
        else:
            body = base64.urlsafe_b64decode(
                payload.get("body", {}).get("data", "")
            ).decode("utf-8", errors="ignore")
        for kw in keywords:
            if kw.upper() in body.upper():
                return kw.upper()
    return None

# ─── BOT LOOP ─────────────────────────────────────────────────────────────────

def bot_loop(user_id, email, lc_session, lc_csrf, gmail_token, log_fn, stop_event):
    def log(msg):
        print(f"[User {user_id}] {msg}")
        log_fn(user_id, msg)

    _state[user_id]     = {}
    _registered[user_id]  = set()
    _12h_sent[user_id]  = set()
    _5min_sent[user_id] = set()

    log("Bot started ✅")

    while not stop_event.is_set():
        try:
            gmail    = get_gmail(gmail_token)
            contests = get_upcoming(lc_session, lc_csrf)
            now_ts   = time.time()

            for c in contests:
                slug  = c["titleSlug"]
                title = c["title"]
                start = c["startTime"]
                hours = (start - now_ts) / 3600
                mins  = (start - now_ts) / 60

                # T-48h: auto register
                if slug not in _registered[user_id]:
                    already = is_registered(lc_session, lc_csrf, slug)
                    if already:
                        _registered[user_id].add(slug)
                    else:
                        ok, err = register(lc_session, lc_csrf, slug)
                        if ok:
                            _registered[user_id].add(slug)
                            start_str = datetime.fromtimestamp(start).strftime("%A, %b %d at %I:%M %p")
                            log(f"✅ Registered for {title}")
                            send_email(gmail, email,
                                f"[LeetCode] ✅ Registered for {title}",
                                f"You're in for '{title}'\n📅 {start_str}\n\n"
                                f"You'll get a check-in 12 hours before. Good luck! 💪"
                            )
                        else:
                            log(f"❌ Register failed for {title}: {err}")

                # T-12h: choice email
                if 11.83 <= hours <= 12.17 and slug not in _12h_sent[user_id]:
                    if is_registered(lc_session, lc_csrf, slug):
                        _12h_sent[user_id].add(slug)
                        log(f"📧 Sending 12h email for {title}")
                        sent_at = time.time()
                        send_email(gmail, email,
                            f"[LeetCode] 12h to go — {title} — your call",
                            f"'{title}' is 12 hours away.\n\n"
                            f"Reply with one of:\n\n"
                            f"  IN    → I'm competing, no more check-ins\n"
                            f"  DROP  → Unregister me now\n"
                            f"  SAFE  → Keep me in but check again at 5 min\n\n"
                            f"No reply = SAFE (5-min trigger stays on)."
                        )
                        # Poll for reply in background
                        def poll_12h(slug=slug, title=title, sent_at=sent_at, start=start):
                            deadline = start - 3600
                            while time.time() < deadline and not stop_event.is_set():
                                time.sleep(60)
                                gm = get_gmail(gmail_token)
                                reply = check_reply(gm, sent_at, ["IN", "DROP", "SAFE"])
                                if reply:
                                    _state[user_id][slug] = reply
                                    log(f"12h reply for {title}: {reply}")
                                    if reply == "DROP":
                                        ok, err = unregister(lc_session, lc_csrf, slug)
                                        if ok:
                                            send_email(gm, email,
                                                f"[LeetCode] ✅ Dropped from {title}",
                                                f"Done — unregistered from '{title}'."
                                            )
                                            log(f"✅ Dropped {title}")
                                        else:
                                            send_email(gm, email,
                                                f"[LeetCode] ❌ Could not drop from {title}",
                                                f"Failed: {err}\nDo it manually at leetcode.com/contest"
                                            )
                                    elif reply == "IN":
                                        send_email(gm, email,
                                            f"[LeetCode] 👍 Locked in for {title}",
                                            f"You're in for '{title}' — no more check-ins. Good luck! 🚀"
                                        )
                                    elif reply == "SAFE":
                                        send_email(gm, email,
                                            f"[LeetCode] 🔔 Safety net on for {title}",
                                            f"Got it — 5-min check still active for '{title}'."
                                        )
                                    break
                            if slug not in _state[user_id]:
                                _state[user_id][slug] = "SAFE"
                                log(f"No 12h reply for {title} — defaulting to SAFE")

                        t = threading.Thread(target=poll_12h, daemon=True)
                        t.start()

                # T-5min: final trigger
                if 4.83 <= mins <= 5.17 and slug not in _5min_sent[user_id]:
                    state = _state[user_id].get(slug, "SAFE")
                    if state not in ("IN", "DROP") and is_registered(lc_session, lc_csrf, slug):
                        _5min_sent[user_id].add(slug)
                        log(f"⏰ 5-min trigger for {title}")
                        start_str = datetime.fromtimestamp(start).strftime("%I:%M %p")
                        sent_at   = time.time()
                        send_email(gmail, email,
                            f"[LeetCode] ⏰ {title} in 5 min — last chance",
                            f"'{title}' starts at {start_str}.\n\n"
                            f"Reply UNREGISTER to drop out.\n"
                            f"Reply anything else to stay in.\n"
                            f"No reply = auto-unregistered at T-1:30."
                        )
                        def poll_5min(slug=slug, title=title, sent_at=sent_at, start=start, start_str=start_str):
                            deadline = start - 90
                            unregistered = False
                            while time.time() < deadline and not stop_event.is_set():
                                time.sleep(10)
                                gm = get_gmail(gmail_token)
                                reply = check_reply(gm, sent_at, ["UNREGISTER"])
                                if reply == "UNREGISTER":
                                    ok, err = unregister(lc_session, lc_csrf, slug)
                                    if ok:
                                        send_email(gm, email,
                                            f"[LeetCode] ✅ Unregistered from {title}",
                                            f"Done — unregistered from '{title}'."
                                        )
                                        log(f"✅ Unregistered from {title}")
                                    else:
                                        send_email(gm, email,
                                            f"[LeetCode] ❌ Unregister failed",
                                            f"Failed: {err}"
                                        )
                                    unregistered = True
                                    _state[user_id][slug] = "DROP"
                                    break
                            if not unregistered and not stop_event.is_set():
                                ok, err = unregister(lc_session, lc_csrf, slug)
                                gm = get_gmail(gmail_token)
                                if ok:
                                    send_email(gm, email,
                                        f"[LeetCode] ✅ Auto-unregistered from {title}",
                                        f"No reply — auto-unregistered from '{title}'."
                                    )
                                    log(f"✅ Auto-unregistered from {title}")
                                else:
                                    send_email(gm, email,
                                        f"[LeetCode] ❌ Auto-unregister failed",
                                        f"Failed: {err}"
                                    )

                        t = threading.Thread(target=poll_5min, daemon=True)
                        t.start()

        except Exception as e:
            log(f"Error: {e}")

        for _ in range(60):
            if stop_event.is_set():
                break
            time.sleep(1)

    log("Bot stopped.")

# ─── START / STOP ─────────────────────────────────────────────────────────────

def start_bot_for_user(user_id, email, lc_session, lc_csrf, gmail_token, log_fn):
    if user_id in _threads and _threads[user_id].is_alive():
        return
    stop_event = threading.Event()
    _stop_events[user_id] = stop_event
    t = threading.Thread(
        target=bot_loop,
        args=(user_id, email, lc_session, lc_csrf, gmail_token, log_fn, stop_event),
        daemon=True,
    )
    _threads[user_id] = t
    t.start()

def stop_bot_for_user(user_id):
    if user_id in _stop_events:
        _stop_events[user_id].set()
    if user_id in _threads:
        _threads[user_id].join(timeout=10)
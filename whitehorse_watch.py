#!/usr/bin/env python3
"""
White Horse Hill watcher: a small Campnab-style availability tracker for DOC NZ.

It checks DOC's booking system for the night(s) you still need at White Horse Hill
Campsite (Aoraki/Mount Cook) and sends an alert to your phone as soon as one opens
up: through a Telegram bot, the ntfy app, or both.

Standard library only: needs Python 3.8+ and nothing else.

    python3 whitehorse_watch.py --check            # show availability around your dates, no alerts
    python3 whitehorse_watch.py --telegram-setup   # find your Telegram chat ID (after messaging your bot)
    python3 whitehorse_watch.py --test-notify      # send a test alert to your phone
    python3 whitehorse_watch.py --loop             # keep checking every ~5 minutes (laptop)
    python3 whitehorse_watch.py                    # check once (what GitHub Actions runs)

Settings come from environment variables (or the matching --options):
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NTFY_TOPIC, WANTED_NIGHTS, MIN_PEOPLE,
FACILITY_ID, BOOKING_URL.
"""

import argparse
import datetime as dt
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- What to watch --------------------------------------------------------------------

# White Horse Hill Campsite. The booking link comes from DOC's own campsite page
# (park 837, facility 2758). The facility id is what the availability API needs.
FACILITY_ID = int(os.environ.get("FACILITY_ID", "2758"))
FACILITY_HINT = "white horse"  # a sanity check against the name the API returns
BOOKING_URL = os.environ.get("BOOKING_URL", "https://bookings.doc.govt.nz/Web/Default.aspx#!park/837/2758")

# A "night" is the date you arrive. You hold 23 and 24 Jan (23-25 Jan), and want
# 22-24 Jan, so the only night you are missing is the night of 22 January.
DEFAULT_NIGHTS = "2027-01-22"

# --- Where to ask ---------------------------------------------------------------------

# DOC's booking site runs on Tyler Technologies' "Recreation Dynamics" (formerly
# UseDirect). This is the JSON endpoint behind its availability calendar.
API_BASE = os.environ.get(
    "WHW_API_BASE",
    "https://prod-nz-rdr.recreation-management.tylerapp.com/nzrdr/rdr/search/occupancygrid",
)
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://prod-nz-rdr.recreation-management.tylerapp.com/",
    "Origin": "https://prod-nz-rdr.recreation-management.tylerapp.com",
    "X-Requested-With": "XMLHttpRequest",
}
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
TELEGRAM_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")

MIN_INTERVAL_MIN = 2      # be polite to DOC: never poll faster than this
ERROR_ALERT_AFTER = 3     # failed checks in a row before you get a "tracker broken" push

# --- Time in New Zealand --------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo
    NZ = ZoneInfo("Pacific/Auckland")
except Exception:  # Python < 3.9, or Windows without tzdata: January is NZDT
    NZ = dt.timezone(dt.timedelta(hours=13))


def nz_today():
    return dt.datetime.now(NZ).date().isoformat()


def add_days(date_str, n):
    return (dt.date.fromisoformat(date_str) + dt.timedelta(days=n)).isoformat()


def nice_date(date_str):
    d = dt.date.fromisoformat(date_str)
    return f"{d:%a} {d.day} {d:%b}"


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


# --- Talking to DOC -------------------------------------------------------------------

class ApiError(Exception):
    pass


def fetch_grid(start, nights, timeout=30):
    """Returns (facility, {date: night}) for `nights` nights from `start`."""
    url = f"{API_BASE}/{FACILITY_ID}/startdate/{start}/nights/{nights}/1"
    req = urllib.request.Request(url, headers=API_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ApiError(f"DOC answered HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"could not reach DOC: {getattr(e, 'reason', e)}") from None

    try:
        data = json.loads(body)
    except ValueError:
        raise ApiError(f"DOC sent something that is not JSON: {body[:200]!r}") from None

    facility = data.get("Facility") if isinstance(data, dict) else None
    dates = facility.get("Dates") if isinstance(facility, dict) else None
    if not isinstance(dates, dict) or not dates:
        raise ApiError(f"unexpected answer from DOC: {body[:300]!r}")

    by_date = {}
    for night in dates.values():
        if isinstance(night, dict) and night.get("Date"):
            by_date[str(night["Date"])[:10]] = night
    if not by_date:
        raise ApiError("DOC's answer had no nights in it")
    return facility, by_date


def fetch_with_retry(start, nights, attempts=3, pause=10):
    for attempt in range(1, attempts + 1):
        try:
            return fetch_grid(start, nights)
        except ApiError as e:
            if attempt == attempts:
                raise
            print(f"  attempt {attempt} failed ({e}); retrying in {pause}s")
            time.sleep(pause)


def is_open(night, min_people):
    """A night counts as bookable when DOC itself marks it available."""
    if not night.get("IsAvailable") or night.get("IsBlocked"):
        return False
    people = night.get("TotalAvailable")
    if isinstance(people, (int, float)) and people < min_people:
        return False
    return True


def describe(night):
    units = night.get("TotalAvailableUnit")
    people = night.get("TotalAvailable")
    bits = []
    if units is not None:
        bits.append(f"{units} unit(s)")
    if people is not None:
        bits.append(f"{people} people")
    return " / ".join(bits) if bits else "no counts"


# --- Telling you ----------------------------------------------------------------------

def desktop_alert(title, message):
    """Best-effort popup and sound on the computer running the script."""
    print("\a", end="", flush=True)
    system = platform.system()
    try:
        if system == "Darwin":
            safe = lambda s: s.replace("\\", "").replace('"', "'")
            subprocess.run(["osascript", "-e",
                            f'display notification "{safe(message)}" with title "{safe(title)}" sound name "Glass"'],
                           check=False, timeout=10)
        elif system == "Windows":
            import winsound
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                time.sleep(0.4)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, message], check=False, timeout=10)
    except Exception as e:  # a missing popup must never stop the tracker
        print(f"  (desktop alert failed: {e})")


def post_json(url, payload, timeout=20):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def telegram_configured(args):
    return bool(args.telegram_token and args.telegram_chat)


def send_telegram(args, title, message, priority):
    payload = {
        "chat_id": args.telegram_chat,
        "text": f"{title}\n\n{message}",
        "disable_web_page_preview": True,
        # Low-priority news ("gone again", "working again") arrives silently.
        "disable_notification": priority <= 2,
        "reply_markup": {"inline_keyboard": [[{"text": "Open DOC booking", "url": BOOKING_URL}]]},
    }
    try:
        post_json(f"{TELEGRAM_API}/bot{args.telegram_token}/sendMessage", payload)
        print("  sent to Telegram")
        return True
    except urllib.error.HTTPError as e:
        # Never print the URL: it contains the bot token.
        detail = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  !! Telegram refused the message: HTTP {e.code} {detail}")
    except Exception as e:
        print(f"  !! Telegram failed: {type(e).__name__}")
    return False


def send_ntfy(args, title, message, priority, tags):
    payload = {
        "topic": args.topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or [],
        "click": BOOKING_URL,
        "actions": [{"action": "view", "label": "Open DOC booking", "url": BOOKING_URL}],
    }
    try:
        post_json(NTFY_SERVER.rstrip("/") + "/", payload)
        print("  sent to ntfy")
        return True
    except Exception as e:
        print(f"  !! ntfy push failed: {e}")
        return False


def push(args, title, message, priority=5, tags=None):
    """Sends the alert to every channel that is set up. Returns True if at least one
    channel delivered it, or if no channel is set up at all (output only)."""
    print(f"\n>>> {title}\n    {message.replace(chr(10), chr(10) + '    ')}\n")
    if args.desktop:
        desktop_alert(title, message.split("\n")[0])

    results = []
    if telegram_configured(args):
        results.append(send_telegram(args, title, message, priority))
    if args.topic:
        results.append(send_ntfy(args, title, message, priority, tags))
    if not results:
        print("  (no Telegram or ntfy set up, so nothing sent to your phone)")
        return True
    return any(results)


def run_telegram_setup(args):
    """Lists the chats that have messaged the bot, so you can copy your chat ID."""
    if not args.telegram_token:
        print("Add your bot token first (the TELEGRAM_BOT_TOKEN secret, or --telegram-token).")
        return 1
    try:
        req = urllib.request.Request(f"{TELEGRAM_API}/bot{args.telegram_token}/getUpdates")
        with urllib.request.urlopen(req, timeout=20) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"!! Telegram said HTTP {e.code}. Is the bot token complete and correct?")
        return 1
    except Exception as e:
        print(f"!! could not reach Telegram: {type(e).__name__}")
        return 1

    chats = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            who = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("title") or ""
            if chat.get("username"):
                who += f" (@{chat['username']})"
            chats[chat["id"]] = who.strip()

    if not chats:
        print("No messages found. Open your bot in Telegram, press Start (or send it any message),")
        print("then run this again within 24 hours.")
        return 1

    lines = [f"Chat ID {cid}  <- {who}" for cid, who in chats.items()]
    print("\n".join(lines))
    print("\nCopy the number into a GitHub secret called TELEGRAM_CHAT_ID.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Your Telegram chat ID\n\n")
            for cid, who in chats.items():
                f.write(f"- **{cid}** ({who})\n")
            f.write("\nCopy this number into a repository secret called `TELEGRAM_CHAT_ID`.\n")
    return 0


# --- Remembering what we already told you ---------------------------------------------

def load_state(path):
    state = {"open": False, "notified_at": None, "error_streak": 0, "error_alerted": False}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                state.update(json.load(f))
        except (OSError, ValueError):
            print(f"  (could not read {path}; starting fresh)")
    return state


def save_state(path, state, before):
    # Only write when something changed, so GitHub Actions does not commit every run.
    if not path or json.dumps(state, sort_keys=True) == before:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


# --- One check ------------------------------------------------------------------------

def window_for(wanted, today):
    """A few nights either side of what you want, for context in the output."""
    start = max(today, add_days(min(wanted), -2))
    end = add_days(max(wanted), 4)
    nights = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    return start, nights


def print_table(facility, by_date, wanted, min_people):
    name = facility.get("Name") or "?"
    print(f"{name} (facility {FACILITY_ID}), {facility.get('TotalUnitCount', '?')} units in total")
    rows = []
    for date in sorted(by_date):
        n = by_date[date]
        status = "OPEN" if is_open(n, min_people) else "full"
        mark = "  <- watching" if date in wanted else ""
        line = f"  {nice_date(date):<11} {status:<5} ({describe(n)}){mark}"
        print(line)
        rows.append((date, status, describe(n), bool(mark)))
    return rows


def write_step_summary(facility, rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"### {facility.get('Name', 'White Horse Hill')}: availability\n\n")
        f.write("| Night | Status | DOC counts | Watching |\n|---|---|---|---|\n")
        for date, status, counts, watching in rows:
            f.write(f"| {nice_date(date)} | {status} | {counts} | {'yes' if watching else ''} |\n")


def check_once(args, state):
    today = nz_today()
    wanted = [d for d in args.nights if d >= today]
    if not wanted:
        print("All watched nights are in the past: nothing left to watch. You can switch the tracker off.")
        return state, True

    start, count = window_for(wanted, today)
    stamp = dt.datetime.now(NZ).strftime("%Y-%m-%d %H:%M")
    print(f"[{stamp} NZ] checking {', '.join(nice_date(d) for d in wanted)} ...")

    try:
        facility, by_date = fetch_with_retry(start, count)
    except ApiError as e:
        print(f"  !! {e}")
        state["error_streak"] = min(state.get("error_streak", 0) + 1, ERROR_ALERT_AFTER)
        if state["error_streak"] >= ERROR_ALERT_AFTER and not state.get("error_alerted"):
            if push(args, "White Horse Hill tracker can't reach DOC",
                    f"The last {ERROR_ALERT_AFTER} checks failed ({e}). Until this is fixed you "
                    "will NOT get availability alerts. You'll get another push when it works again.",
                    priority=3, tags=["warning"]):
                state["error_alerted"] = True
        return state, False

    if state.get("error_alerted"):
        push(args, "White Horse Hill tracker is working again",
             "DOC is answering again, so alerts are back on.", priority=2, tags=["white_check_mark"])
    state["error_streak"] = 0
    state["error_alerted"] = False

    name = str(facility.get("Name") or "")
    if FACILITY_HINT not in name.lower():
        print(f"  !! DOC returned '{name}' instead of White Horse Hill. Check FACILITY_ID.")

    print_table(facility, by_date, wanted, args.min_people)

    missing = [d for d in wanted if d not in by_date]
    if missing:
        print(f"  !! DOC did not return {', '.join(missing)}; treating as not available")
    open_now = not missing and all(is_open(by_date[d], args.min_people) for d in wanted)

    nights_txt = " + ".join(nice_date(d) for d in wanted)
    counts_txt = "\n".join(f"{nice_date(d)}: {describe(by_date[d])}" for d in wanted if d in by_date)

    if open_now and not state.get("open"):
        if push(args, f"White Horse Hill: {nights_txt} is AVAILABLE",
                f"Book it now, before someone else does: use the Open DOC booking button.\n{counts_txt}",
                priority=5, tags=["rotating_light", "camping"]):
            state["open"] = True
            state["notified_at"] = now_utc().isoformat()

    elif open_now and args.remind_minutes > 0:
        last = state.get("notified_at")
        minutes = (now_utc() - dt.datetime.fromisoformat(last)).total_seconds() / 60 if last else 1e9
        if minutes >= args.remind_minutes:
            if push(args, f"Still open: White Horse Hill {nights_txt}",
                    f"Still bookable after {int(minutes)} min: use the Open DOC booking button.\n{counts_txt}",
                    priority=4, tags=["camping"]):
                state["notified_at"] = now_utc().isoformat()
        else:
            print(f"  still open; next reminder in about {int(args.remind_minutes - minutes)} min")

    elif not open_now and state.get("open"):
        push(args, f"Gone again: White Horse Hill {nights_txt}",
             "Someone else booked it (or you did!). Still watching for the next opening.",
             priority=2, tags=["hourglass"])
        state["open"] = False
        state["notified_at"] = None

    else:
        print("  not available yet; still watching" if not open_now else "  open (already told you)")

    return state, False


# --- Modes ----------------------------------------------------------------------------

def run_check_only(args):
    today = nz_today()
    wanted = [d for d in args.nights if d >= today] or args.nights
    start, count = window_for(wanted, today)
    try:
        facility, by_date = fetch_with_retry(start, count)
    except ApiError as e:
        print(f"!! {e}")
        return 1
    rows = print_table(facility, by_date, wanted, args.min_people)
    write_step_summary(facility, rows)
    name = str(facility.get("Name") or "")
    if FACILITY_HINT not in name.lower():
        print(f"!! DOC returned '{name}' instead of White Horse Hill. Check FACILITY_ID.")
        return 1
    print("\nThe tracker can read DOC's availability. All good.")
    return 0


def run_test_notify(args):
    if args.telegram_token and not args.telegram_chat:
        print("Telegram: bot token found but no TELEGRAM_CHAT_ID yet. Run the telegram-setup mode first.")
    if not telegram_configured(args) and not args.topic:
        print("Set up Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) or ntfy (NTFY_TOPIC) first.")
        return 1
    title = "Test: White Horse Hill tracker"
    message = "If you can read this on your phone, alerts work. The button opens the DOC booking page."
    print(f"\n>>> {title}\n")
    # Test each channel on its own, so a broken one turns the run red.
    ok = True
    if telegram_configured(args):
        ok = send_telegram(args, title, message, 4) and ok
    if args.topic:
        ok = send_ntfy(args, title, message, 4, ["camping"]) and ok
    return 0 if ok else 1


def run_watch(args):
    before = None
    state = load_state(args.state)
    if not args.loop:
        before = json.dumps(state, sort_keys=True)
        state, _ = check_once(args, state)
        save_state(args.state, state, before)
        return 0

    interval = max(args.interval, MIN_INTERVAL_MIN)
    print(f"Watching every ~{interval:g} min. Leave this window open; Ctrl+C to stop.\n")
    while True:
        before = json.dumps(state, sort_keys=True)
        state, finished = check_once(args, state)
        save_state(args.state, state, before)
        if finished:
            return 0
        time.sleep(interval * 60 * random.uniform(0.8, 1.2))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Watch DOC's White Horse Hill Campsite for an opening and push-alert your phone.")
    p.add_argument("--nights", default=os.environ.get("WANTED_NIGHTS") or DEFAULT_NIGHTS,
                   help="night(s) you need, as arrival dates, comma separated (default: %(default)s)")
    p.add_argument("--telegram-token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                   help="your Telegram bot token (from @BotFather)")
    p.add_argument("--telegram-chat", default=os.environ.get("TELEGRAM_CHAT_ID", ""),
                   help="your Telegram chat ID (find it with --telegram-setup)")
    p.add_argument("--topic", default=os.environ.get("NTFY_TOPIC", ""),
                   help="your ntfy topic (the name you subscribed to in the ntfy app)")
    p.add_argument("--min-people", type=int, default=int(os.environ.get("MIN_PEOPLE") or 1),
                   help="only alert if DOC reports at least this many free places (default: %(default)s)")
    p.add_argument("--state", default="state.json", help="file that remembers what you were already told")
    p.add_argument("--loop", action="store_true", help="keep running and check every --interval minutes")
    p.add_argument("--interval", type=float, default=5, help="minutes between checks in --loop mode (default: 5)")
    p.add_argument("--remind-minutes", type=float, default=30,
                   help="while it stays open, remind every N minutes (0 = off, default: 30)")
    p.add_argument("--desktop", action="store_true", help="also beep and show a popup on this computer")
    p.add_argument("--check", action="store_true", help="just show current availability and exit (no alerts)")
    p.add_argument("--test-notify", action="store_true", help="send a test alert to your phone and exit")
    p.add_argument("--telegram-setup", action="store_true",
                   help="show the chat ID of whoever messaged your bot, and exit")
    args = p.parse_args(argv)

    nights = [n.strip() for n in args.nights.split(",") if n.strip()]
    for n in nights:
        try:
            dt.date.fromisoformat(n)
        except ValueError:
            p.error(f"'{n}' is not a date like 2027-01-22")
    args.nights = sorted(set(nights))
    if not args.nights:
        p.error("give at least one night to watch")
    args.topic = args.topic.strip()
    args.telegram_token = args.telegram_token.strip()
    args.telegram_chat = args.telegram_chat.strip()
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.check:
            return run_check_only(args)
        if args.test_notify:
            return run_test_notify(args)
        if args.telegram_setup:
            return run_telegram_setup(args)
        return run_watch(args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

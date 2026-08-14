#!/usr/bin/env python3
"""
Cineplex seat watcher -- phone edition.

Watches one specific showtime's seat map and pushes to your phone the moment a
seat opens up inside the block you actually want. Unlike the desktop watcher
this replaces, it never opens a browser, never pops a window and never beeps:
the only output is stdout plus an ntfy push, so it runs fine on a headless box
you're SSH'd into, a VPS, or a container while you're out with just a phone.

Default target: The Odyssey, IMAX, 6:40 PM, the coming Saturday,
                Scotiabank Theatre Toronto, rows E-L, seats 6-34.

The seat data comes from the same two endpoints cineplex.com's own seat-map
page calls, both of which answer unauthenticated:

    /v1/theatre/{theatre}/showtime/{session}/seat-layout        (static per show)
    /v1/theatre/{theatre}/showtime/{session}/seat-availability  (live)

seat-layout gives every physical seat -- row label, seat label, grid column,
seat type. seat-availability maps seat id -> "Available" / "Occupied". Neither
alone is enough: availability also contains ids for grid positions that aren't
real seats, so a seat only counts if the layout listed it.

Zero third-party dependencies -- stdlib only.

Usage:
    python cineplex_seat_watch.py              # watch until a seat opens
    python cineplex_seat_watch.py --once       # single check; exit 0 if seats
    python cineplex_seat_watch.py --seats      # print the seat map right now
    python cineplex_seat_watch.py --list       # every showtime on the date
    python cineplex_seat_watch.py --rows E-L --seat-range 6-34 --together 2
"""

import argparse
import gzip
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# CONFIG -- edit this block
# --------------------------------------------------------------------------

THEATRE_ID = 7402                     # Scotiabank Theatre Toronto
THEATRE_LABEL = "Scotiabank Theatre Toronto"

TARGET_DATE = ""                      # "" = the coming Saturday; else YYYY-MM-DD
MOVIE_CONTAINS = "odyssey"            # case-insensitive substring of movie name
REQUIRE_EXPERIENCE = "IMAX"           # "" to accept any format
TARGET_TIME = "18:40"                 # 6:40 PM, theatre-local
TIME_TOLERANCE_MIN = 10               # keep tight -- you asked for this show

# The seat block you'll actually sit in.
ROW_FIRST = "E"                       # inclusive; rows are letters, no row I
ROW_LAST = "L"                        # inclusive
SEAT_FIRST = 6                        # inclusive, by printed seat number
SEAT_LAST = 34                        # inclusive

# Wheelchair spaces and their companion seats are excluded by default -- they
# exist inside this row/seat block but shouldn't be taken speculatively.
ALLOWED_SEAT_TYPES = ("Standard",)

# Require this many seats side by side in one row before alerting.
# 1 = alert on any single seat.
MIN_TOGETHER = 1

POLL_SECONDS = 30                     # base interval; jitter added automatically
POLL_JITTER_SECONDS = 5

# Phone push via ntfy.sh -- free, no signup. Same topic the desktop watcher
# used, so the app on your phone is already subscribed.
NTFY_TOPIC = "odyssey-imax-x4lty08gwx"

# Re-push this often while seats stay available, so one missed buzz isn't fatal.
REALERT_SECONDS = 300

# Proof-of-life. A watcher that died quietly looks exactly like a watcher that
# has found nothing yet, and from a phone you can't see the process at all --
# so it pushes a silent heartbeat to a SEPARATE topic. Subscribe to that topic
# too and the timestamp on its newest message is your "still running" light.
# Keeping it off the alert topic matters: a buzz there always means seats.
# "every-check" mirrors every poll to the status topic, so the topic reads as
# a live log rather than a liveness light. "interval" throttles to
# HEARTBEAT_SECONDS. "off" is silent.
HEARTBEAT_MODE = "interval"
HEARTBEAT_SECONDS = 300               # only consulted when mode is "interval"
HEARTBEAT_TOPIC = ""                  # "" = NTFY_TOPIC + "-status"

# Names this watcher in its heartbeats. With more than one running -- say a
# live one on a box you're sat at and a scheduled one elsewhere -- an
# unlabelled heartbeat tells you something is alive but not what, which is
# the wrong half of the answer when you're deciding whether to restart.
LABEL = "live"

# --------------------------------------------------------------------------

API_HOST = "https://apis.cineplex.com"
SHOWTIMES = API_HOST + "/prod/cpx/theatrical/api/v1/showtimes"
TICKETING = API_HOST + "/prod/ticketing/api/v1"
SUBSCRIPTION_KEY = "dcdac5601d864addbc2675a2e96cb1f8"

HEADERS = {
    "Ocp-Apim-Subscription-Key": SUBSCRIPTION_KEY,
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "identity",
    "Accept-Language": "en-CA,en;q=0.9",
    "Origin": "https://www.cineplex.com",
    "Referer": "https://www.cineplex.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}

# Layout is static for a given showtime; refetch occasionally in case the
# auditorium is re-laid out mid-run.
LAYOUT_TTL_SECONDS = 1800


HERE = os.path.dirname(os.path.abspath(__file__))
BEAT_PATH = os.path.join(HERE, "seat_watch_heartbeat.txt")


def log(msg):
    print("%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


def write_beat(text):
    """Local proof-of-life, for when you're back at a machine with the disk."""
    try:
        with open(BEAT_PATH, "w", encoding="utf-8") as fh:
            fh.write("%s  %s\n"
                     % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def heartbeat_topic():
    if HEARTBEAT_TOPIC:
        return HEARTBEAT_TOPIC
    return (NTFY_TOPIC + "-status") if NTFY_TOPIC else ""


def coming_saturday(today=None):
    """The next Saturday, or today if today is already Saturday."""
    today = today or date.today()
    return (today + timedelta(days=(5 - today.weekday()) % 7)).isoformat()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25,
                                context=ssl.create_default_context()) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    # The API sometimes gzips even though we ask for identity -- an upstream
    # proxy decides, not us -- and urllib doesn't decompress on its own. Sniff
    # the magic number as well as the header, since the header goes missing
    # when something in the middle rewrites it.
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def fetch_showtimes(theatre_id, day):
    qs = urllib.parse.urlencode(
        {"language": "en", "locationId": theatre_id, "date": day}
    )
    return _get(SHOWTIMES + "?" + qs)


def fetch_seat_layout(theatre_id, session_id):
    return _get("%s/theatre/%s/showtime/%s/seat-layout"
                % (TICKETING, theatre_id, session_id))


def fetch_seat_availability(theatre_id, session_id):
    payload = _get("%s/theatre/%s/showtime/%s/seat-availability"
                   % (TICKETING, theatre_id, session_id))
    return payload.get("seatAvailabilities", {}) or {}


def minutes_of(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def extract_sessions(payload):
    """Flatten the nested showtimes response into one dict per showtime."""
    out = {}
    for theatre in payload or []:
        for day in theatre.get("dates", []) or []:
            for movie in day.get("movies", []) or []:
                for exp in movie.get("experiences", []) or []:
                    types = exp.get("experienceTypes", []) or []
                    for s in exp.get("sessions", []) or []:
                        rec = out.setdefault(
                            s.get("vistaSessionId"),
                            {
                                "movie": movie.get("name", ""),
                                "session_id": s.get("vistaSessionId"),
                                "start": s.get("showStartDateTime") or "",
                                "auditorium": s.get("auditorium") or "",
                                "seats": s.get("seatsRemaining"),
                                "sold_out": bool(s.get("isSoldOut")),
                                "online": bool(s.get("isShowtimeEnabledOnline")),
                                "reserved": bool(s.get("isReservedSeating")),
                                "buy_url": s.get("ticketingUrl") or "",
                                "seatmap_url": s.get("seatMapUrl") or "",
                                "experiences": set(),
                            },
                        )
                        rec["experiences"].update(types)
    for rec in out.values():
        rec["experiences"] = sorted(rec["experiences"])
    return sorted(out.values(), key=lambda r: r["start"])


def find_target_session(sessions):
    """The one showtime we're watching, or None if it isn't listed yet."""
    want = MOVIE_CONTAINS.lower()
    target = minutes_of(TARGET_TIME)
    best = None
    for s in sessions:
        if want not in s["movie"].lower():
            continue
        if REQUIRE_EXPERIENCE and not any(
            REQUIRE_EXPERIENCE.lower() == e.lower() for e in s["experiences"]
        ):
            continue
        try:
            delta = abs(minutes_of(s["start"].split("T")[1][:5]) - target)
        except (IndexError, ValueError):
            continue
        if delta > TIME_TOLERANCE_MIN:
            continue
        if not s["online"] or s["sold_out"]:
            continue
        if best is None or delta < best[0]:
            best = (delta, s)
    return best[1] if best else None


# --------------------------------------------------------------------------
# Seat filtering
# --------------------------------------------------------------------------

def seat_number(label):
    """Trailing digits of a seat label. 'E12' -> 12, 'CW3' -> 3."""
    m = re.search(r"(\d+)$", label or "")
    return int(m.group(1)) if m else None


def row_in_range(row_label):
    """Single-letter row labels between ROW_FIRST and ROW_LAST inclusive.

    The auditorium skips row I, so an alphabetical span is the honest test --
    it picks up whichever letters actually exist in between.
    """
    if not row_label or len(row_label) != 1 or not row_label.isalpha():
        return False
    return ROW_FIRST.upper() <= row_label.upper() <= ROW_LAST.upper()


def walk_seats(layout):
    """Yield (row_label, seat dict) for every real seat in the auditorium.

    Seats live under several sibling groups (standardSeats, dboxSeats,
    balconySeats); spacer rows carry a null label.
    """
    for group in (layout or {}).values():
        if not isinstance(group, dict) or "rows" not in group:
            continue
        for row in group.get("rows", []) or []:
            for seat in row.get("seats", []) or []:
                yield row.get("label"), seat


def matching_seats(layout, availability):
    """Available seats inside the wanted block, grouped by row.

    Returns {row_label: [seat, ...]} with each list sorted by grid column, so
    physically adjacent seats end up adjacent in the list.
    """
    rows = {}
    for row_label, seat in walk_seats(layout):
        if not row_in_range(row_label):
            continue
        if seat.get("type") not in ALLOWED_SEAT_TYPES:
            continue
        if availability.get(seat.get("id")) != "Available":
            continue
        num = seat_number(seat.get("label"))
        if num is None or not (SEAT_FIRST <= num <= SEAT_LAST):
            continue
        rows.setdefault(row_label, []).append(seat)
    for seats in rows.values():
        seats.sort(key=lambda s: s.get("column", 0))
    return dict(sorted(rows.items()))


def contiguous_runs(seats):
    """Split one row's seats into physically side-by-side runs.

    Adjacency is grid column, not seat number: an aisle shows up as a column
    gap even when the printed numbers keep counting.
    """
    runs, run = [], []
    for seat in seats:
        if run and seat.get("column", 0) != run[-1].get("column", 0) + 1:
            runs.append(run)
            run = []
        run.append(seat)
    if run:
        runs.append(run)
    return runs


def qualifying_runs(rows):
    """Every run at least MIN_TOGETHER seats long, as (row, [seats])."""
    out = []
    for row_label, seats in rows.items():
        for run in contiguous_runs(seats):
            if len(run) >= MIN_TOGETHER:
                out.append((row_label, run))
    return out


def describe_rows(rows):
    lines = []
    for row_label, seats in rows.items():
        runs = contiguous_runs(seats)
        pretty = "  ".join(
            "+".join(s["label"] for s in run) for run in runs
        )
        lines.append("row %s: %d seat(s)  %s" % (row_label, len(seats), pretty))
    return lines


def block_label():
    return "rows %s-%s, seats %d-%d" % (ROW_FIRST, ROW_LAST, SEAT_FIRST, SEAT_LAST)


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

def ntfy(title, body, click=None, buy=None, priority="urgent", attempts=5):
    """Push, retrying through a network blip. Returns True if it landed.

    Worth retrying properly: this is the one message that matters, and a
    dropped connection at the wrong moment is the difference between knowing
    a seat opened and not. The caller re-queues on a False.
    """
    if not NTFY_TOPIC:
        return False
    delay = 2
    for attempt in range(1, attempts + 1):
        if _ntfy_once(title, body, click, buy, priority):
            return True
        if attempt < attempts:
            log("  -> push retry %d/%d in %ds" % (attempt, attempts - 1, delay))
            time.sleep(delay)
            delay = min(30, delay * 2)
    log("  !! push failed after %d attempts -- will retry next check" % attempts)
    return False


def _ntfy_once(title, body, click=None, buy=None, priority="urgent"):
    try:
        headers = {
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": priority,
            "Tags": "clapper,rotating_light",
        }
        actions = []
        if click:
            headers["Click"] = click
            actions.append("view, Seat map, %s" % click)
        if buy and buy != click:
            actions.append("view, Buy tickets, %s" % buy)
        if actions:
            headers["Actions"] = "; ".join(actions)
        req = urllib.request.Request(
            "https://ntfy.sh/" + NTFY_TOPIC,
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()
        log("  -> phone push sent (ntfy topic %s)" % NTFY_TOPIC)
        return True
    except Exception as exc:
        log("  -> ntfy failed: %r" % (exc,))
        return False


def heartbeat(text, tag="hourglass_flowing_sand"):
    """Quiet push to the status topic.

    Priority low, not min: both are silent -- no sound, no vibration -- but
    min buries the notification under the fold, and the point of this topic
    is that you can see it.
    """
    topic = heartbeat_topic()
    write_beat(text)
    if not topic or HEARTBEAT_MODE == "off":
        return
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/" + topic,
            data=text.encode("utf-8"),
            headers={
                # The label goes in the title because ntfy's notification list
                # shows titles first -- you want to read which watcher this is
                # without opening anything.
                "Title": "Alive: %s" % LABEL,
                "Priority": "low",
                "Tags": tag,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as exc:
        log("  -> heartbeat push failed: %r" % (exc,))


def alert(session, rows, target_date):
    runs = qualifying_runs(rows)
    total = sum(len(seats) for seats in rows.values())
    best = max((len(r) for _, r in runs), default=0)

    title = "SEATS OPEN -- %s %s" % (MOVIE_CONTAINS.title(), TARGET_TIME)
    lines = describe_rows(rows)
    body = "%s\n%s %s | %s\n%s\n\n%s" % (
        session["movie"], target_date, TARGET_TIME, session["auditorium"],
        "%d seat(s) in %s, best run %d together" % (total, block_label(), best),
        "\n".join(lines),
    )

    log("=" * 68)
    log("  *** " + title + " ***")
    for line in lines:
        log("  " + line)
    log("  %d seat(s) in %s, best run %d together"
        % (total, block_label(), best))
    log("=" * 68)
    log("  seat map -> " + session["seatmap_url"])
    log("  buy      -> " + session["buy_url"])
    return ntfy(title, body, click=session["seatmap_url"],
                buy=session["buy_url"])


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def resolve_session(target_date):
    sessions = extract_sessions(fetch_showtimes(THEATRE_ID, target_date))
    return find_target_session(sessions), sessions


def do_list(target_date):
    _, sessions = resolve_session(target_date)
    if not sessions:
        print("Nothing listed at %s on %s yet." % (THEATRE_LABEL, target_date))
        return 1
    print("%s -- %s  (%d showtimes)"
          % (THEATRE_LABEL, target_date, len(sessions)))
    for s in sessions:
        print("  %s | %s | %s | %s | seats=%s"
              % (s["movie"], s["start"].replace("T", " "),
                 ",".join(s["experiences"]) or "Regular",
                 s["auditorium"], s["seats"]))
    return 0


def do_seats(target_date):
    """Print the whole seat map, so you can sanity-check the filter."""
    session, _ = resolve_session(target_date)
    if not session:
        print("No %s %s show near %s on %s yet."
              % (MOVIE_CONTAINS, REQUIRE_EXPERIENCE, TARGET_TIME, target_date))
        return 1

    layout = fetch_seat_layout(THEATRE_ID, session["session_id"])
    availability = fetch_seat_availability(THEATRE_ID, session["session_id"])

    print("%s | %s | %s | session %s"
          % (session["movie"], session["start"].replace("T", " "),
             session["auditorium"], session["session_id"]))
    print("Wanted block: %s (min %d together)\n"
          % (block_label(), MIN_TOGETHER))

    by_row = {}
    for row_label, seat in walk_seats(layout):
        if row_label:
            by_row.setdefault(row_label, []).append(seat)

    for row_label in sorted(by_row):
        seats = sorted(by_row[row_label], key=lambda s: s.get("column", 0))
        free = [s for s in seats if availability.get(s.get("id")) == "Available"]
        mark = ">>" if row_in_range(row_label) else "  "
        print("%s row %-2s  %2d/%2d free   %s"
              % (mark, row_label, len(free), len(seats),
                 " ".join(s["label"] for s in free[:40])))

    rows = matching_seats(layout, availability)
    print()
    if rows:
        print("MATCHING %s:" % block_label())
        for line in describe_rows(rows):
            print("  " + line)
        return 0
    print("No seats available in %s right now." % block_label())
    return 1


def do_status():
    """Is a watcher alive? Reads the heartbeat file this box's watcher writes.

    Only meaningful on the machine running the watcher -- from a phone, use
    the heartbeat topic in the ntfy app instead.
    """
    if not os.path.exists(BEAT_PATH):
        print("No heartbeat file at %s -- no watcher has run here." % BEAT_PATH)
        return 1
    text = open(BEAT_PATH, encoding="utf-8").read().strip()
    try:
        stamp = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        print(text)
        return 1
    age = (datetime.now() - stamp).total_seconds()
    print(text)
    print("Last beat %.0fs ago." % age)
    if age > max(180, POLL_SECONDS * 4):
        print("STALE -- the watcher looks dead. Restart it.")
        return 1
    print("Alive.")
    return 0


def do_watch(once=False):
    target_date = TARGET_DATE or coming_saturday()
    log("Watching %s | %s | %s ~%s %s"
        % (THEATRE_LABEL, target_date, MOVIE_CONTAINS, TARGET_TIME,
           REQUIRE_EXPERIENCE or "any format"))
    log("Block: %s | min %d together | every ~%ds"
        % (block_label(), MIN_TOGETHER, POLL_SECONDS))
    if not NTFY_TOPIC:
        log("NOTE: NTFY_TOPIC is empty -- console only, no phone push.")
    if HEARTBEAT_MODE != "off" and heartbeat_topic():
        log("Status: ntfy topic %s, %s (silent)"
            % (heartbeat_topic(),
               "every check" if HEARTBEAT_MODE == "every-check"
               else "every %dm" % (HEARTBEAT_SECONDS // 60)))
        log("Alerts: ntfy topic %s, urgent (sound)" % NTFY_TOPIC)

    checks = 0
    fails = 0
    last_alert = 0.0
    last_seen = None            # set of seat labels at the previous alert
    last_beat = 0.0
    layout = None
    layout_fetched = 0.0
    layout_for = None
    announced_missing = False

    heartbeat("started %s\nwatching %s %s %s in %s"
              % (datetime.now().strftime("%H:%M:%S"), target_date, TARGET_TIME,
                 REQUIRE_EXPERIENCE, block_label()), tag="white_check_mark")
    last_beat = time.time()

    while True:
        checks += 1
        try:
            session, _ = resolve_session(target_date)

            if not session:
                if not announced_missing:
                    announced_missing = True
                    msg = ("%s %s near %s is not listed on %s yet"
                           % (MOVIE_CONTAINS.title(),
                              REQUIRE_EXPERIENCE or "", TARGET_TIME,
                              target_date))
                    log("*** " + msg + " -- watching for it to appear.")
                    ntfy("Showtime not listed", msg, priority="default")
                if once:
                    return 1
                time.sleep(POLL_SECONDS + random.uniform(0, POLL_JITTER_SECONDS))
                continue

            sid = session["session_id"]
            if layout is None or layout_for != sid \
                    or time.time() - layout_fetched > LAYOUT_TTL_SECONDS:
                layout = fetch_seat_layout(THEATRE_ID, sid)
                layout_fetched = time.time()
                layout_for = sid

            availability = fetch_seat_availability(THEATRE_ID, sid)
            fails = 0
        except urllib.error.HTTPError as exc:
            fails += 1
            log("check %d: HTTP %s" % (checks, exc.code))
            if exc.code in (401, 403):
                log("  !! Key may have rotated, or you're being rate-limited. "
                    "Backing off.")
            time.sleep(min(300, 20 * fails))
            continue
        except Exception as exc:
            fails += 1
            log("check %d: %r" % (checks, exc))
            time.sleep(min(300, 15 * fails))
            continue

        rows = matching_seats(layout, availability)
        runs = qualifying_runs(rows)

        # Mirror the check to the status topic before handling the result, so
        # a --once run (the scheduled job's shape) still reports in, and so a
        # check is never lost to an early return below.
        due = (HEARTBEAT_MODE == "every-check"
               or (HEARTBEAT_MODE == "interval"
                   and time.time() - last_beat >= HEARTBEAT_SECONDS))
        if due:
            free = sum(len(v) for v in rows.values())
            heartbeat("check %d at %s\n%d seat(s) free in %s\n"
                      "%s left in the auditorium overall\n"
                      "watching %s %s %s"
                      % (checks, datetime.now().strftime("%H:%M:%S"), free,
                         block_label(), session["seats"], target_date,
                         TARGET_TIME, session["auditorium"]),
                      tag="tickets" if runs else "mag")
            last_beat = time.time()

        if runs:
            # Only the seats in qualifying runs count as the hit.
            hit_rows = {}
            for row_label, run in runs:
                hit_rows.setdefault(row_label, []).extend(run)
            labels = frozenset(s["label"] for r in hit_rows.values() for s in r)

            now = time.time()
            changed = labels != last_seen
            if changed or now - last_alert >= REALERT_SECONDS:
                delivered = alert(session, hit_rows, target_date)
                # An undelivered alert is the one failure that costs you the
                # seat, so don't let the re-alert timer swallow the retry:
                # leave the clock untouched and try again next check.
                last_alert = now if delivered else 0.0
                last_seen = labels if delivered else None
                if not once:
                    log("Re-pushing every %ds while seats hold, and "
                        "immediately if the set changes. Ctrl+C when booked."
                        % REALERT_SECONDS)
            if once:
                return 0
        else:
            last_seen = None
            tick = ("check %d -- %s: %s sold out in %s (%s seats left elsewhere)"
                    % (checks, TARGET_TIME, block_label(),
                       session["auditorium"], session["seats"]))
            log(tick)
            write_beat(tick)
            if once:
                return 1

        time.sleep(POLL_SECONDS + random.uniform(0, POLL_JITTER_SECONDS))


def parse_row_span(text):
    m = re.match(r"^\s*([A-Za-z])\s*-\s*([A-Za-z])\s*$", text or "")
    if not m:
        raise argparse.ArgumentTypeError("rows must look like E-L")
    a, b = m.group(1).upper(), m.group(2).upper()
    return (a, b) if a <= b else (b, a)


def parse_num_span(text):
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", text or "")
    if not m:
        raise argparse.ArgumentTypeError("seat range must look like 6-34")
    a, b = int(m.group(1)), int(m.group(2))
    return (a, b) if a <= b else (b, a)


def main():
    ap = argparse.ArgumentParser(
        description="Watch one Cineplex showtime for seats in a row/seat block.")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--seats", action="store_true",
                    help="print the current seat map and exit")
    ap.add_argument("--list", action="store_true",
                    help="list all showtimes on the date")
    ap.add_argument("--interval", type=int, help="override poll seconds")
    ap.add_argument("--date", help="override date, YYYY-MM-DD (default: "
                                   "the coming Saturday)")
    ap.add_argument("--time", help="override showtime, HH:MM (24h)")
    ap.add_argument("--tolerance", type=int,
                    help="minutes of slack around --time")
    ap.add_argument("--movie", help="override movie substring")
    ap.add_argument("--experience", help="override format, e.g. IMAX; '' for any")
    ap.add_argument("--theatre", type=int, help="override theatre id")
    ap.add_argument("--rows", type=parse_row_span,
                    help="row span, e.g. E-L")
    ap.add_argument("--seat-range", type=parse_num_span,
                    help="seat number span, e.g. 6-34")
    ap.add_argument("--together", type=int,
                    help="require N seats side by side (default 1)")
    ap.add_argument("--ntfy", help="override ntfy topic; '' to disable push")
    ap.add_argument("--status", action="store_true",
                    help="report whether a watcher is beating, then exit")
    ap.add_argument("--heartbeat", type=int, metavar="MIN",
                    help="throttle status pushes to one every MIN minutes "
                         "(0 disables them; default is one per check)")
    ap.add_argument("--heartbeat-topic",
                    help="ntfy topic for heartbeats "
                         "(default: <topic>-status)")
    ap.add_argument("--label",
                    help="name this watcher in its heartbeats, so several "
                         "running at once stay tellable apart (default: live)")
    ap.add_argument("--any-seat-type", action="store_true",
                    help="also match wheelchair/companion seats")
    args = ap.parse_args()

    global POLL_SECONDS, TARGET_DATE, TARGET_TIME, MOVIE_CONTAINS
    global REQUIRE_EXPERIENCE, THEATRE_ID, TIME_TOLERANCE_MIN
    global ROW_FIRST, ROW_LAST, SEAT_FIRST, SEAT_LAST, MIN_TOGETHER
    global NTFY_TOPIC, ALLOWED_SEAT_TYPES
    global HEARTBEAT_SECONDS, HEARTBEAT_TOPIC, LABEL, HEARTBEAT_MODE

    if args.interval:
        POLL_SECONDS = max(10, args.interval)
    if args.date:
        TARGET_DATE = args.date
    if args.time:
        TARGET_TIME = args.time
    if args.tolerance is not None:
        TIME_TOLERANCE_MIN = args.tolerance
    if args.movie:
        MOVIE_CONTAINS = args.movie
    if args.experience is not None:
        REQUIRE_EXPERIENCE = args.experience
    if args.theatre:
        THEATRE_ID = args.theatre
    if args.rows:
        ROW_FIRST, ROW_LAST = args.rows
    if args.seat_range:
        SEAT_FIRST, SEAT_LAST = args.seat_range
    if args.together:
        MIN_TOGETHER = max(1, args.together)
    if args.ntfy is not None:
        NTFY_TOPIC = args.ntfy
    if args.any_seat_type:
        ALLOWED_SEAT_TYPES = ("Standard", "Wheelchair", "Companion", "DBox")
    if args.heartbeat is not None:
        if args.heartbeat <= 0:
            HEARTBEAT_MODE = "off"
        else:
            HEARTBEAT_MODE = "interval"
            HEARTBEAT_SECONDS = args.heartbeat * 60
    if args.heartbeat_topic:
        HEARTBEAT_TOPIC = args.heartbeat_topic
    if args.label:
        LABEL = args.label

    target_date = TARGET_DATE or coming_saturday()

    try:
        if args.status:
            return do_status()
        if args.list:
            return do_list(target_date)
        if args.seats:
            return do_seats(target_date)
        return do_watch(once=args.once)
    except KeyboardInterrupt:
        log("Stopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main() or 0)

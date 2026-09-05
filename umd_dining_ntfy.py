#!/usr/bin/env python3
"""
umd_dining_ntfy.py
==================

Scrape nutrition.umd.edu and push an ntfy notification listing the foods
available for lunch and dinner at:

  * South Campus Dining Hall   (locationNum 16)
  * Yahentamitsi Dining Hall   (locationNum 19)

Meant to be run once every morning at 08:00 for the *current* day.  Run it
from Windows Task Scheduler / cron (recommended), or use ``--daemon`` to let
the script sit running and fire itself at 08:00 local time every day.

Configuration is via environment variables (or a .env file next to this
script).  The only required one is NTFY_TOPIC.

    NTFY_TOPIC              ntfy topic to publish to            (required)
    NTFY_SERVER             ntfy server base URL                (default https://ntfy.sh)
    NTFY_TOKEN              bearer token for protected topics   (optional)
    NTFY_PRIORITY          min|low|default|high|max             (default default)
    NTFY_MAX_BYTES         truncate each message body to N bytes(default 3900)

    MEALS                  comma list of meals to report        (default lunch,dinner)
    NOTIFY_MODE            per-meal | per-hall | single         (default per-meal)
    INCLUDE_SIDES          1 to include "... Sides" stations    (default 0)
    MAX_ITEMS_PER_STATION  cap items listed per station, 0=all  (default 12)

Examples
--------
    # one shot for today (what Task Scheduler / cron should call)
    python umd_dining_ntfy.py

    # print instead of sending, useful for testing
    python umd_dining_ntfy.py --dry-run

    # a specific date
    python umd_dining_ntfy.py --date 2026-09-08 --dry-run

    # run forever, firing every day at 08:00 local
    python umd_dining_ntfy.py --daemon

    # register / remove a Windows Scheduled Task that runs daily at 08:00
    python umd_dining_ntfy.py --install-task
    python umd_dining_ntfy.py --uninstall-task
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MENU_URL = "https://nutrition.umd.edu/"

DINING_HALLS = [
    ("South Campus Dining Hall", 16),
    ("Yahentamitsi Dining Hall", 19),
]

# On weekends UMD serves "Brunch" instead of "Breakfast" + "Lunch".  Treat
# Brunch as the "lunch" slot so a request for lunch still gets something.
MEAL_ALIASES = {
    "breakfast": ["Breakfast"],
    "brunch": ["Brunch"],
    "lunch": ["Lunch", "Brunch"],
    "dinner": ["Dinner"],
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TASK_NAME = "UMD Dining ntfy"
USER_AGENT = "umd-dining-ntfy/1.0 (+https://nutrition.umd.edu scraper)"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def _load_dotenv() -> None:
    """Minimal .env loader (KEY=VALUE lines) so no extra dependency is needed."""
    path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


@dataclass
class Config:
    topic: str = ""
    server: str = "https://ntfy.sh"
    token: str = ""
    priority: str = "default"
    max_bytes: int = 3900
    meals: list[str] = field(default_factory=lambda: ["lunch", "dinner"])
    notify_mode: str = "per-meal"
    include_sides: bool = False
    max_items_per_station: int = 12

    @classmethod
    def from_env(cls) -> "Config":
        meals = [
            m.strip().lower()
            for m in os.environ.get("MEALS", "lunch,dinner").split(",")
            if m.strip()
        ]
        for m in meals:
            if m not in MEAL_ALIASES:
                raise SystemExit(
                    f"Unknown meal {m!r} in MEALS. Valid: {', '.join(MEAL_ALIASES)}"
                )
        mode = os.environ.get("NOTIFY_MODE", "per-meal").strip().lower()
        if mode not in {"per-meal", "per-hall", "single"}:
            raise SystemExit("NOTIFY_MODE must be per-meal, per-hall or single")
        return cls(
            topic=os.environ.get("NTFY_TOPIC", "").strip(),
            server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            token=os.environ.get("NTFY_TOKEN", "").strip(),
            priority=os.environ.get("NTFY_PRIORITY", "default").strip(),
            max_bytes=int(os.environ.get("NTFY_MAX_BYTES", "3900")),
            meals=meals,
            notify_mode=mode,
            include_sides=os.environ.get("INCLUDE_SIDES", "0").strip()
            not in {"", "0", "false", "no"},
            max_items_per_station=int(os.environ.get("MAX_ITEMS_PER_STATION", "12")),
        )


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #


@dataclass
class Station:
    name: str
    items: list[str]


def umd_date(d: dt.date) -> str:
    """nutrition.umd.edu wants M/D/YYYY (no zero padding required)."""
    return f"{d.month}/{d.day}/{d.year}"


def fetch_day(location_num: int, d: dt.date, session: requests.Session) -> dict[str, list[Station]]:
    """Return {meal_label: [Station, ...]} for one hall on one day.

    meal_label is exactly what the site uses that day: "Breakfast"/"Lunch"/
    "Dinner" on weekdays, "Brunch"/"Dinner" on weekends.  Empty dict means the
    hall published no menu for that day (closed / not posted yet).
    """
    resp = session.get(
        MENU_URL,
        params={"locationNum": location_num, "dtdate": umd_date(d)},
        timeout=30,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    menus: dict[str, list[Station]] = {}
    for tab in soup.select("ul.nav-tabs a.nav-link"):
        label = tab.get_text(strip=True)
        pane_id = tab.get("href", "").lstrip("#")
        if not label or not pane_id:
            continue
        pane = soup.find("div", id=pane_id)
        if pane is None:
            continue
        stations: list[Station] = []
        for card in pane.select("div.card"):
            title_el = card.select_one(".card-title")
            station_name = title_el.get_text(strip=True) if title_el else "Menu"
            items: list[str] = []
            seen: set[str] = set()
            for a in card.select("a.menu-item-name"):
                name = " ".join(a.get_text(strip=True).split())
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    items.append(name)
            if items:
                stations.append(Station(station_name, items))
        if stations:
            menus[label] = stations
    return menus


def pick_meal(menus: dict[str, list[Station]], meal: str) -> tuple[str, list[Station]] | None:
    """Resolve a requested meal ("lunch") to an actual served period."""
    for candidate in MEAL_ALIASES[meal]:
        if candidate in menus:
            return candidate, menus[candidate]
    return None


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def filter_stations(stations: list[Station], cfg: Config) -> list[Station]:
    out = []
    for st in stations:
        if not cfg.include_sides and st.name.strip().lower().endswith("sides"):
            continue
        items = st.items
        if cfg.max_items_per_station > 0 and len(items) > cfg.max_items_per_station:
            hidden = len(items) - cfg.max_items_per_station
            items = items[: cfg.max_items_per_station] + [f"...(+{hidden} more)"]
        out.append(Station(st.name, items))
    return out


def render_meal_block(meal_label: str, stations: list[Station]) -> str:
    lines = []
    for st in stations:
        lines.append(f"**{st.name}**")
        lines.append(", ".join(st.items))
        lines.append("")
    return "\n".join(lines).rstrip()


def clamp(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[: max_bytes - 20].decode("utf-8", "ignore")
    return truncated.rstrip() + "\n...(truncated)"


@dataclass
class Notification:
    title: str
    body: str
    tags: list[str] = field(default_factory=list)


def build_notifications(
    d: dt.date,
    hall_menus: list[tuple[str, dict[str, list[Station]]]],
    cfg: Config,
) -> list[Notification]:
    """hall_menus: [(hall_name, {meal_label: [Station]}), ...]"""
    nice_date = d.strftime("%a %b %-d") if os.name != "nt" else d.strftime("%a %b ") + str(d.day)
    notes: list[Notification] = []

    # Gather resolved (hall, requested_meal) -> (served_label, stations)
    resolved: list[tuple[str, str, str, list[Station]]] = []
    for hall_name, menus in hall_menus:
        for meal in cfg.meals:
            picked = pick_meal(menus, meal)
            if picked is None:
                resolved.append((hall_name, meal, "", []))
                continue
            served_label, stations = picked
            resolved.append(
                (hall_name, meal, served_label, filter_stations(stations, cfg))
            )

    if cfg.notify_mode == "per-meal":
        for hall_name, meal, served_label, stations in resolved:
            short_hall = hall_name.replace(" Dining Hall", "")
            meal_title = meal.capitalize()
            if served_label and served_label.lower() != meal:
                meal_title = f"{meal.capitalize()} ({served_label})"
            if not stations:
                body = f"_No {meal} menu posted for {nice_date}._"
            else:
                body = render_meal_block(served_label or meal, stations)
            notes.append(
                Notification(
                    title=f"{short_hall} - {meal_title} - {nice_date}",
                    body=clamp(body, cfg.max_bytes),
                    tags=["fork_and_knife"],
                )
            )
        return notes

    if cfg.notify_mode == "per-hall":
        by_hall: dict[str, list[str]] = {}
        for hall_name, meal, served_label, stations in resolved:
            block_title = (served_label or meal).capitalize()
            if stations:
                block = f"## {block_title}\n" + render_meal_block(served_label or meal, stations)
            else:
                block = f"## {block_title}\n_Not posted for {nice_date}._"
            by_hall.setdefault(hall_name, []).append(block)
        for hall_name, blocks in by_hall.items():
            short_hall = hall_name.replace(" Dining Hall", "")
            notes.append(
                Notification(
                    title=f"{short_hall} - {nice_date}",
                    body=clamp("\n\n".join(blocks), cfg.max_bytes),
                    tags=["fork_and_knife"],
                )
            )
        return notes

    # single
    chunks: list[str] = []
    for hall_name, meal, served_label, stations in resolved:
        header = f"# {hall_name} - {(served_label or meal).capitalize()}"
        if stations:
            chunks.append(header + "\n" + render_meal_block(served_label or meal, stations))
        else:
            chunks.append(header + f"\n_Not posted for {nice_date}._")
    notes.append(
        Notification(
            title=f"UMD Dining - {nice_date}",
            body=clamp("\n\n".join(chunks), cfg.max_bytes),
            tags=["fork_and_knife"],
        )
    )
    return notes


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


def send_notification(note: Notification, cfg: Config, session: requests.Session) -> None:
    headers = {
        "Title": note.title.encode("utf-8"),
        "Priority": cfg.priority,
        "Markdown": "yes",
    }
    if note.tags:
        headers["Tags"] = ",".join(note.tags)
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    url = f"{cfg.server}/{cfg.topic}"
    resp = session.post(url, data=note.body.encode("utf-8"), headers=headers, timeout=30)
    resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run_once(cfg: Config, d: dt.date, dry_run: bool) -> int:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    hall_menus: list[tuple[str, dict[str, list[Station]]]] = []
    for hall_name, loc in DINING_HALLS:
        try:
            menus = fetch_day(loc, d, session)
        except Exception as exc:  # network / parse failure for one hall
            log(f"ERROR fetching {hall_name}: {exc}")
            menus = {}
        hall_menus.append((hall_name, menus))
        log(f"{hall_name}: {', '.join(f'{k}={sum(len(s.items) for s in v)}it' for k, v in menus.items()) or 'no menu'}")

    notes = build_notifications(d, hall_menus, cfg)

    if dry_run:
        for note in notes:
            print("=" * 70)
            print(f"TITLE: {note.title}")
            print(f"TAGS : {', '.join(note.tags)}")
            print("-" * 70)
            print(note.body)
        print("=" * 70)
        print(f"[dry-run] {len(notes)} notification(s) would be sent "
              f"to {cfg.server}/{cfg.topic or '<NTFY_TOPIC unset>'}")
        return 0

    if not cfg.topic:
        raise SystemExit("NTFY_TOPIC is not set. Put it in the environment or a .env file.")

    failures = 0
    for note in notes:
        try:
            send_notification(note, cfg, session)
            log(f"sent: {note.title} ({len(note.body.encode('utf-8'))} bytes)")
        except Exception as exc:
            failures += 1
            log(f"ERROR sending {note.title!r}: {exc}")
    return 1 if failures else 0


def log(msg: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(SCRIPT_DIR, "umd_dining_ntfy.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Daemon mode
# --------------------------------------------------------------------------- #


def seconds_until(hour: int, minute: int) -> float:
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def run_daemon(cfg: Config, hour: int, minute: int) -> None:
    log(f"daemon started; will fire every day at {hour:02d}:{minute:02d} local time")
    while True:
        delay = seconds_until(hour, minute)
        log(f"sleeping {delay / 3600:.2f}h until next run")
        time.sleep(delay)
        try:
            run_once(cfg, dt.date.today(), dry_run=False)
        except Exception as exc:
            log(f"ERROR during scheduled run: {exc}")
        time.sleep(60)  # avoid a double-fire within the same minute


# --------------------------------------------------------------------------- #
# Windows Task Scheduler helpers
# --------------------------------------------------------------------------- #


def _task_time(value: str) -> tuple[int, int]:
    hh, _, mm = value.partition(":")
    return int(hh), int(mm or 0)


def install_task(at: str) -> None:
    import subprocess

    if os.name != "nt":
        raise SystemExit("--install-task is Windows-only. On Linux/macOS use cron:\n"
                         f"  0 {at.split(':')[0]} * * *  {sys.executable} {os.path.abspath(__file__)}")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    runner = pythonw if os.path.isfile(pythonw) else sys.executable
    script = os.path.abspath(__file__)
    cmd = [
        "schtasks", "/Create", "/F",
        "/SC", "DAILY",
        "/ST", at,
        "/TN", TASK_NAME,
        "/TR", f'"{runner}" "{script}" --once',
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f'\nInstalled scheduled task "{TASK_NAME}" (daily at {at}).')
    print("Make sure NTFY_TOPIC is set as a *system* or *user* environment "
          "variable, or filled into the .env file next to the script, so the "
          "task can see it.")


def uninstall_task() -> None:
    import subprocess

    if os.name != "nt":
        raise SystemExit("--uninstall-task is Windows-only.")
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], check=True)
    print(f'Removed scheduled task "{TASK_NAME}".')


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Push UMD dining-hall lunch/dinner menus to ntfy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--once", action="store_true",
                        help="fetch and send a single time (default action)")
    parser.add_argument("--daemon", action="store_true",
                        help="stay running, fire every day at --at")
    parser.add_argument("--at", default="08:00", metavar="HH:MM",
                        help="time of day for --daemon / --install-task (default 08:00)")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="report this date instead of today (testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print notifications instead of sending them")
    parser.add_argument("--install-task", action="store_true",
                        help="register a Windows Scheduled Task (daily at --at)")
    parser.add_argument("--uninstall-task", action="store_true",
                        help="remove the Windows Scheduled Task")
    args = parser.parse_args(argv)

    _load_dotenv()

    if args.install_task:
        install_task(args.at)
        return 0
    if args.uninstall_task:
        uninstall_task()
        return 0

    cfg = Config.from_env()

    if args.daemon:
        hour, minute = _task_time(args.at)
        run_daemon(cfg, hour, minute)
        return 0

    target_date = (
        dt.datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else dt.date.today()
    )
    return run_once(cfg, target_date, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

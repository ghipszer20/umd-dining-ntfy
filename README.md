# UMD Dining → ntfy

Scrapes [nutrition.umd.edu](https://nutrition.umd.edu) every morning and pushes an
[ntfy](https://ntfy.sh) notification listing the foods available for **lunch** and
**dinner** at:

- **South Campus Dining Hall** (`locationNum=16`)
- **Yahentamitsi Dining Hall** (`locationNum=19`)

On weekends the halls serve *Brunch* instead of Breakfast + Lunch; the script
reports Brunch in the "lunch" slot so you still get a midday menu.

## Setup

```powershell
cd C:\Users\24GHi\source\repos\umd-dining-ntfy
py -m pip install -r requirements.txt
copy .env.example .env
notepad .env        # set NTFY_TOPIC to your topic
```

Then subscribe to that same topic name in the ntfy app on your phone
(Android / iOS) or at <https://ntfy.sh/app>.

## Use

```powershell
py umd_dining_ntfy.py --dry-run        # print today's notifications, send nothing
py umd_dining_ntfy.py                  # fetch today and send  (what the scheduler runs)
py umd_dining_ntfy.py --date 2026-09-08 --dry-run   # a specific day, for testing
```

## Run it at 8 AM every day

### Windows Task Scheduler (recommended)

```powershell
py umd_dining_ntfy.py --install-task           # daily at 08:00
py umd_dining_ntfy.py --install-task --at 07:30
py umd_dining_ntfy.py --uninstall-task
```

This registers a task named **"UMD Dining ntfy"** that runs
`pythonw.exe umd_dining_ntfy.py --once` every day. Because it uses `.env` for
config, the task works even when no shell environment is loaded. View or edit it
later in *Task Scheduler* (`taskschd.msc`).

> The task runs only while the PC is on. If it's asleep/off at 08:00, Task
> Scheduler runs it at the next wake by default. For a machine that's often off,
> host this on an always-on box or a small cloud VM instead.

### Linux / macOS (cron)

```cron
0 8 * * *  /usr/bin/python3 /path/to/umd_dining_ntfy.py --once
```

### Daemon mode (no scheduler)

```powershell
py umd_dining_ntfy.py --daemon            # stays running, fires at 08:00 local daily
py umd_dining_ntfy.py --daemon --at 08:00
```

## Configuration

All via environment variables or `.env` (see `.env.example` for the full list).
Highlights:

| Variable                | Default        | Meaning                                            |
|-------------------------|----------------|----------------------------------------------------|
| `NTFY_TOPIC`            | *(required)*   | ntfy topic to publish to                           |
| `NTFY_SERVER`           | `https://ntfy.sh` | ntfy server base URL                            |
| `MEALS`                 | `lunch,dinner` | meals to report                                    |
| `NOTIFY_MODE`           | `per-meal`     | `per-meal` (≤4/day) · `per-hall` (2/day) · `single`|
| `INCLUDE_SIDES`         | `0`            | include `"... Sides"` stations                     |
| `MAX_ITEMS_PER_STATION` | `12`           | cap items per station (`0` = all)                  |
| `NTFY_MAX_BYTES`        | `3900`         | truncate body to fit ntfy's ~4 KB message cap      |

## Notes

- The site is a server-rendered FoodPro/CBORD app. The scraper reads the
  `ul.nav-tabs` meal tabs and each `div.card` (`.card-title` = station,
  `a.menu-item-name` = item). If UMD redesigns the page, `fetch_day()` is the
  one function to update.
- Menus for a given day sometimes aren't posted until a few days prior; a hall
  with nothing posted produces a "no menu posted" notification rather than an
  error.
- A run log is written to `umd_dining_ntfy.log` next to the script.

# SSS — SO / STO Status

A single-purpose internal tool: log in, check the live status of Sales Orders
(SO) and Stock Transfer Orders (STO) — Order status, Packslip status, Invoice
number — and track individual items via a watchlist. Nothing else. It's
deliberately independent of any other internal tool (own login, own
accounts, own process).

## What it does

- **Home** — a calendar view of SO/STO activity by day.
- **Search** — look up any SO/STO by number, customer, or invoice; see full
  order/packslip/invoice status and a history log of what changed and when.
- **Alerts** — watch specific items by code; see live matches against
  in-flight SO/STO lines. Production Managers can mark a watched item as
  Produced.
- **Settings** — theme, default landing tab, and (admin only) the data-sync
  controls below.
- **CMS** (admin only) — manage user accounts, per-user tab visibility,
  Production Manager grants, and upload the source data files.

Layout is responsive: a left sidebar nav (collapsible) on desktop/tablet, a
bottom tab bar on phone-width screens — same page, same code, both work.

## Data source

Reads from two Excel exports (`Dispatch SO.xls`, `Dispach STO.xls`) plus an
Item Master CSV for the watchlist search. Locally these are read from a
fixed folder on disk; on a host without that folder (e.g. Railway), an admin
uploads them instead via CMS → Data Files, which also triggers an immediate
sync.

**Data refreshes itself** — a background thread re-syncs on a timer
(default every 20 minutes, admin-configurable down to a 5-minute minimum via
Settings → Data Sync). Nobody, including the admin, needs to click anything
for routine use. A manual "Sync Now" button remains for admins who want to
force an immediate refresh — e.g. right after manually dropping a new file
onto disk instead of uploading through CMS.

History (the change log Search shows per document) only tracks changes seen
*since the first sync after this app started tracking that document* —
Excel exports are snapshots, not logs, so there's no way to backfill history
that predates the first sync.

## Accounts & permissions

- **Login**: username/password, case-insensitive on both (accounts created
  before that change need one password reset via CMS to fully benefit —
  see below).
- **Roles**: `admin` (full CMS access) or `user`.
- **Per-user tabs**: an admin can hide/show Home, Search, Alerts, and
  Settings per account from CMS → Users → Edit user. CMS itself is not
  part of this list — it's tied to the `admin` role directly.
- **Production Managers**: a separate grant (CMS) for who can mark a
  watched item "Produced" — distinct from the `admin` role, since not
  every admin runs production.
- Admins can reset any user's password from the same "Edit user" modal
  used for tabs (CMS → Users → Edit tabs → New password field).

## Running it locally

```
pip install -r requirements.txt
python app.py
```

Prints both a `localhost` and a LAN URL on startup (auto-detected) — the
LAN one is what other devices on the same network use to reach it. Reads
`PORT` from the environment if set (default `5050` locally).

In production here, this runs as a persistent Windows service (via NSSM)
rather than a manually-run terminal — don't run `python app.py` by hand
while the service is active, they'll fight over the same port.

## Configuration (environment variables)

| Variable | Purpose | Local default |
|---|---|---|
| `PORT` | port to listen on | `5050` |
| `SECRET_KEY` | Flask session signing key | auto-generated once into `sss_secret.key` |
| `DATA_DIR` | where the SQLite DBs and uploaded source files live | this file's own folder |

On a host like Railway, set `DATA_DIR` to a mounted persistent volume path
(e.g. `/data`) and `SECRET_KEY` to a fixed random string — otherwise every
redeploy loses accounts, synced data, and sessions.

## Tech

Flask + SQLite (one DB for accounts, one for tracker data) + pandas/xlrd for
reading the Excel exports + waitress as the production WSGI server.

## Project layout

```
app.py              Flask routes — auth, pages, all /api/so-sto/* and /api/admin/* endpoints
sss_auth.py          Session login/logout, @login_required / @admin_required decorators
sss_auth_db.py        User accounts DB (username, password hash, role, per-user tabs)
so_sto_db.py          Tracker data DB (snapshot, history, item master, watchlist, settings)
so_sto_ingest.py       Reads the Excel/CSV source files, diffs against the previous snapshot
templates/
  sss_login.html        Login page
  sss_admin.html         CMS (admin) page
  so_sto.html            The main app — all four tabs, one page, responsive
```

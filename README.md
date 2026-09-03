# SSS Tool — SO / STO Status

> Project folder: `SSS Tool` (renamed from `SO`). The app itself is branded
> **SSS** (SO / STO Status).

A single-purpose internal tool: log in, check the live status of Sales Orders
(SO) and Stock Transfer Orders (STO) — Order status, Packslip status, Invoice
number — and track individual items via a watchlist. Nothing else. It's
deliberately independent of any other internal tool (own login, own
accounts, own process).

## What it does

- **Home** — a calendar view of SO/STO activity by day.
- **Search** — look up any SO/STO by number, customer, or invoice; see full
  order/packslip/invoice status and a history log of what changed and when.
  Typing an *exact* SO/STO number opens its detail straight away from
  whatever tab you're already on (Home included) — no need to land on
  Search first. Partial queries still show a results list on the Search tab.
- **Alerts** — watch specific items by code; see live matches against
  in-flight SO/STO lines. Production Managers can mark a watched item as
  Produced.
- **Settings** — theme, default landing tab, **Export** (CSV of your watchlist
  or current Search results), and (admin only) the data-sync controls below.
- **CMS** (admin only) — manage user accounts, per-user tab visibility,
  Production Manager grants, and upload the source data files. Shares the
  same sidebar nav as the rest of the app (with CMS shown active), rather
  than being a dead-end page.

### Look & feel
The UI follows the same navy/blue corporate palette, Calibri typography, and
sidebar/topbar treatment as the Peps Industries Product Contribution Tool, so
the two feel like one family of internal tools rather than unrelated apps.
Applies across login, the main app, and CMS, in both light and dark theme.

### Installable on phone (PWA)
The app is a Progressive Web App, so it opens like a native app on **both
iPhone (Safari → Add to Home Screen) and Android (Chrome → Install/Add to
Home Screen)** — no App Store needed. It also works offline: the last-seen
data is cached by the service worker, so Search/Alerts/History stay readable
with no signal. The layout is unchanged — same sidebar on desktop, same bottom
tab bar on phones.

Layout is responsive: a left sidebar nav (collapsible) on desktop/tablet, a
bottom tab bar on phone-width screens — same page, same code, both work.

## Data source

Reads from two Excel exports (`Dispatch SO.xls`, `Dispach STO.xls`) plus an
Item Master CSV for the watchlist search. Locally these are read from the
`Input/` and `Item Master/` folders next to `App/` (relative paths, so the
project can live anywhere or be renamed); on a host without those folders
(e.g. Railway), an admin uploads them instead via CMS → Data Files, which also
triggers an immediate sync.

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
                    + PWA routes (/manifest.webmanifest, /sw.js) and CSV export endpoints
sss_auth.py          Session login/logout, @login_required / @admin_required decorators
sss_auth_db.py        User accounts DB (username, password hash, role, per-user tabs)
so_sto_db.py          Tracker data DB (snapshot, history, item master, watchlist, settings)
so_sto_ingest.py       Reads the Excel/CSV source files, diffs against the previous snapshot
pwa/
  manifest.webmanifest  PWA manifest (name, icons, standalone display)
  sw.js                 Service worker (app-shell + offline API caching)
static/
  icon-*.png, apple-touch-icon.png   Generated app icons
templates/
  sss_login.html        Login page
  sss_admin.html         CMS (admin) page
  so_sto.html            The main app — all four tabs, one page, responsive
```


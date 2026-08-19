# BruinWatch

A Discord bot that looks up UCLA classes and DMs you the moment a section you're
watching changes enrollment status.

Formerly `speedchat-bot`. The Japanese-dictionary and Toontown Speedchat commands
have been removed; what's left is the class tracker, rebuilt.

> The screenshots in `images/` are from the old `~`-prefix interface and haven't
> been retaken since the move to slash commands.

---

## What it does

| Command | |
|---|---|
| `/search subject number [term]` | Show a class's sections; pick one from the dropdown to watch it. Subject and number autocomplete. |
| `/subject subject [term]` | Page through every course offered under a subject area. |
| `/history subject number [term]` | Enrollment over time for each section, as a sparkline plus status transitions. |
| `/watchlist` | Review what you're watching; remove sections inline. |
| `/unwatch subject number` | Stop watching every section of a course. |
| `/notify subject number spots` | Also ping when an open section drops to N spots or fewer. |
| `/alias set\|remove\|list` | Shorthands for awkward subject codes — `CS` → `COM SCI`. |
| `/about` | What the bot does. |
| `/admin …` | Owner-only: `stats`, `sync`, `scraper`, `enrollment-window`, `resync-commands`. |

You'll get a DM when a watched section's status changes — `Full → Open`,
`Open → Waitlist`, and so on — and, if you set one, when it crosses your
spots-left threshold.

## How it polls

The registrar has no API, so BruinWatch scrapes the public
[Schedule of Classes](https://sa.ucla.edu/ro/Public/SOC/). The scraping design
follows [hotseat.io](https://github.com/hotseatio/hotseat.io), which has run
against the same source in production for years.

Four tiers, each matched to how fast its data actually moves:

| Tier | Cadence | Scope |
|---|---|---|
| Terms | daily | the term dropdown |
| Subject areas | weekly | per active term |
| Course catalog | daily | every subject in every active term |
| All sections | hourly | full sweep; this is what builds the enrollment history |
| **Watched sections** | **adaptive, 30s – 15m** | only sections somebody subscribes to |

The watched tier settles at 2 minutes during campus daytime hours and drops to
15 minutes overnight, and speeds up to 30 seconds while an enrollment pass
window is open. Consecutive failures trip a circuit breaker that backs off and
DMs the owner rather than hammering a registrar that's already having a bad day.

> **Pass windows are entered by hand.** The registrar publishes them on a page
> whose URL changes yearly, so — as hotseat.io also does — you record them with
> `/admin enrollment-window`. Until you do, there is no 30-second tier: polling
> just alternates between the 2-minute and 15-minute rates.

Two properties matter for load:

- **Requests scale with watched sections, not subscribers.** The poller reads
  `DISTINCT section_id` from `subscriptions` and fetches each *course* once, so
  fifty people watching the same lecture cost one request, not fifty.
- **Concurrency is capped** by a process-wide semaphore (default 10) over a
  single pooled HTTP/2 client, with exponential backoff and a descriptive
  User-Agent.

### Change detection

Ported from hotseat's `SaveSection`. In one transaction, per section:

1. read the previously stored enrollment numbers;
2. upsert the section;
3. if **any** number moved → append a row to `enrollment_data` (this is the
   history `/history` charts);
4. if the **enrollment status** changed → queue a DM per subscriber.

A section we've never seen before seeds the history series but notifies nobody.
Notifications go into a `notification_outbox` table and are delivered by a
separate loop, so a restart mid-fan-out can neither drop nor duplicate a DM.

The rules live in [`services/changes.py`](src/bruinwatch/services/changes.py) as
pure functions, and are tested there without a database.

### Why there's no course-model cache

The registrar's AJAX endpoints want an opaque JSON `model` blob with a base64
`Token`. The obvious way to get one is to scrape it off a results page — which
is what this bot used to do, caching a blob for every course in the catalog.

It turns out that blob is a pure function of the subject code and catalog
number, so [`registrar/model.py`](src/bruinwatch/registrar/model.py) just builds
it. That deleted the entire cache, and with it the multi-minute "reloading the
class names JSON" step that used to block commands.

`tests/test_model.py` checks our output against models the registrar emitted for
itself, captured in the fixtures — if the construction ever drifts, the tests
fail loudly instead of every scrape silently returning nothing.

---

## Backfilling past terms

The registrar serves any term code you ask for, back to **Fall 1999** — far
beyond the eight terms its dropdown advertises. `bruinwatch-backfill` walks
those terms and records what was offered, by whom, at what capacity, and how
full each section ended up:

```bash
uv run bruinwatch-backfill --from 23W --to 27S --dry-run   # print the plan
uv run bruinwatch-backfill --from 23W --to 27S             # ~13 h at 5 req/s
```

Resumable — each (term, subject) unit is recorded on completion, so an
interrupted run picks up where it stopped and re-running is safe.

**It cannot recover enrollment history.** The archive holds a single frozen
snapshot per section, not a time series. Fill curves, time-to-full and anything
about *how* demand built exist only for terms the live scraper watched. A
backfill is a different dataset from the hourly sweep, not an extension of it.

Measured costs (probed against the live registrar, not estimated):

| | |
|---|---|
| Terms available | 99F → present, ~135 terms |
| Subjects per term | 168 |
| Courses per subject | ~62 |
| Requests per term | ~11,000 |
| Median latency / payload | 21 ms / 10.8 KB |
| 2023 → present (22 terms) | ~242k requests, ~13 h at 5/s, ~2.5 GB |
| Whole archive (135 terms) | ~1.5M requests, ~3.5 days at 5/s, ~16 GB |
| Storage for the whole archive | ~5 GB Postgres |

`--rate` is a politeness budget, not a throughput knob. The registrar answers in
21 ms and would let you finish the whole archive in an hour; don't. About 10% of
historical sections come back with capacity `0` — some departments close their
sections by department at term end — so demand ratios exclude them.

---

## The stats site

The bot serves a read-only web UI from the same process, on
`BRUINWATCH_HEALTHCHECK_PORT` (8080 by default):

| Route | |
|---|---|
| `/stats` | Term overview — how much of the catalog is closed, most in-demand courses, hardest subjects, fastest-filling sections |
| `/stats/courses` | Every course with recorded history |
| `/stats/course/{subject}/{number}` | Enrollment-over-time curves per section, plus term-over-term peaks |
| `/api/stats/summary` | The same numbers as JSON |
| `/api/stats/course/{subject}/{number}` | Full history for one course as JSON |
| `/healthz` | Gateway and scraper state |

The headline measures:

- **Demand ratio** — enrolled plus waitlisted, over capacity, aggregated across
  a course's sections. Above 1.0× means more students want in than the room holds.
- **Subject pressure** — the share of a subject's sections that are full,
  waitlisted or closed.
- **Time to fill** — how long a section took to close. Measured from *this bot's
  first observation*, not from when the registrar opened enrollment, because we
  cannot know the latter for a section we met mid-term. The UI says so wherever
  the number appears.
- **Term over term** — a course's peak fill in each term. Needs two terms of
  collected history before it says anything.

**It starts empty.** `enrollment_data` only fills as the scraper runs; expect a
thin page for the first day and a genuinely useful one after a term. Every page
says what it is waiting for rather than rendering a blank chart.

Charts are server-rendered inline SVG — no CDN, no build step, no client-side
fetch — against a palette validated for colour-vision deficiency in both light
and dark mode. Every chart ships a table view beside it, and lines carry direct
end labels, so no value is ever reachable by colour alone.

---

## Running it

Needs a Discord bot token and PostgreSQL. No privileged gateway intents:
everything is a slash command, so `message_content` is not required.

### Docker

```bash
cp .env.example .env      # fill in BRUINWATCH_DISCORD_TOKEN and BRUINWATCH_OWNER_ID
docker compose up --build
```

Compose brings up Postgres, runs `alembic upgrade head`, then starts the bot.
The stats site is on <http://localhost:8080/stats> and the health endpoint on
<http://localhost:8080/healthz>.

### Locally

```bash
uv sync
cp .env.example .env
createdb bruinwatch                       # or point BRUINWATCH_DATABASE_URL elsewhere
uv run alembic upgrade head
uv run python -m bruinwatch
```

### Just the stats site, with no bot and no data

```bash
uv run bruinwatch-demo          # http://127.0.0.1:8099/stats
```

Seeds synthetic sections and fill curves into an in-process PGlite and serves
the site against them — no Discord token, no Postgres, no scraped history.
Useful because real history takes a term to accumulate.

**Every page it serves carries a DEMO DATA banner.** The instructor names are
placeholders (`Instructor A.`) rather than real faculty, so a demo screenshot
can't be mistaken for a claim about a real person. Pass `--database-url` to seed
a real Postgres instead — it drops and recreates the schema, so point it
somewhere disposable.

Set `BRUINWATCH_DEV_GUILD_ID` to sync slash commands to one guild instantly;
global commands take about an hour to propagate.

### Configuration

Every setting is a `BRUINWATCH_`-prefixed environment variable — see
[`.env.example`](.env.example) and [`config.py`](src/bruinwatch/config.py).
Notable ones: `MAX_CONCURRENCY`, `REQUEST_TIMEOUT`, `CIRCUIT_BREAKER_THRESHOLD`,
and `SCHEDULER_ENABLED=false` to run the bot with no background scraping.

---

## Development

```bash
uv sync
uv run ruff check src tests
uv run ruff format src tests
uv run mypy src
uv run pytest
```

**The whole suite runs with no Docker and no Postgres installed.** Parser tests
use saved registrar responses in `tests/fixtures/`, HTTP is stubbed with
`respx`, and the database tests get a real PostgreSQL from
[PGlite](https://pglite.dev) — Postgres compiled to WebAssembly, started
in-process over a TCP socket. The only requirement is Node on your PATH. (This
is the same approach the sibling `ll-predictions` repo uses in
`scripts/dev-up.ts`.)

The schema is PostgreSQL-specific — `TEXT[]` columns, `INSERT ... ON CONFLICT
... RETURNING`, a partial index — so those tests need a genuine engine rather
than SQLite pretending to be one.

To test against a real server instead, point the tests at it:

```bash
createdb bruinwatch_test
BRUINWATCH_TEST_DATABASE_URL=postgresql+asyncpg://localhost/bruinwatch_test uv run pytest
```

That takes precedence over PGlite. Without Node and without the variable, the
database tests skip with an explanatory message — set
`BRUINWATCH_REQUIRE_TEST_DB=1` to turn that skip into a failure, which is what
CI does. CI runs the suite twice: once against a Postgres 16 service container
and once against PGlite, so neither path can rot.

`tests/test_schema.py` compares the Alembic migration's DDL against the
SQLAlchemy models, so a model change without a migration fails CI. It needs no
database at all.

See [`tests/postgres.py`](tests/postgres.py) for how the database is chosen and
which PGlite quirks the fixtures work around.

### Checking the live registrar

When a scrape looks wrong, ask the registrar directly — no bot, no database:

```bash
uv run python -m bruinwatch.scripts.probe "COM SCI" 32 --term 26F
uv run python -m bruinwatch.scripts.probe MATH 31A --term 26F --json
```

It prints the `model` it built and everything parsed out of the response. If it
finds nothing for a class you know is offered, the registrar changed its markup.
CI runs this weekly as a canary.

### Layout

```
src/bruinwatch/
  registrar/   pure UCLA layer: model building, HTTP client, parsers. No DB, no Discord.
  db/          SQLAlchemy models, queries, Alembic migrations.
  services/    change detection, scheduling, notification delivery.
  analytics.py the stats queries. Returns dataclasses; renders nothing.
  web/         the stats site: inline-SVG charts, page shell, routes.
  cogs/        slash commands.
  ui/          embeds and interactive components.
```

`registrar/` and `services/changes.py` have no framework dependencies, which is
why the interesting logic is easy to test.

---

## A note on scraping

This hits a public, unauthenticated page that any student can load in a browser,
at a rate well below what a person clicking refresh would generate: an hourly
full sweep, bounded concurrency, and faster polling only for classes somebody
explicitly asked about. Please keep it that way if you fork it.

## License

MIT.

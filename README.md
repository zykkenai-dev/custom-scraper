# Custom Lead Scraper

High-ticket lead scraper for an AI agency offering premium websites, custom
software, AI automation, and CRM builds. Discovers businesses in target
 niches and harvests outreach contact points: **emails, WhatsApp numbers,
Instagram handles, LinkedIn profiles, and phone numbers**.

## Niches (high-ticket, pre-configured)

| Niche            | Targets                                   |
| ---------------- | ----------------------------------------- |
| `real_estate`    | Real estate & property development        |
| `finance`        | Wealth management, financial advisory     |
| `healthcare`     | Private clinics, cosmetic, med-spa        |
| `legal`          | Corporate law firms, attorneys            |
| `saas`           | B2B SaaS & software companies             |
| `ecommerce`      | D2C brands & online stores                |
| `coaching`       | High-ticket coaching & consulting         |
| `automotive`     | Luxury dealers, exotic car dealerships    |
| `hospitality`    | Luxury hotels, resorts, fine dining       |

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # then paste your keys
python main.py --dry-run    # validate the pipeline without scraping
python main.py --list-niches
```

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest            # unit tests plus a local HTTP end-to-end test
```

The suite covers the extractors (emails, Cloudflare decoding, phones,
WhatsApp/Instagram/LinkedIn), filters and deny lists, the lead model and
quality scoring, CSV/JSON round-trips and merge/dedupe, dashboard log parsing,
and the pure helper functions of the search/collector/social modules. It
catches regressions without contacting external services.

### Run a scrape

```bash
python main.py --niche real_estate finance --max 30 --out data/leads.csv
python main.py --niche saas --max 100 --out data/leads.json
python main.py                      # all niches, default limits
```

### Seeded discovery mode

If the free engines are bot-checking your IP, use your own shortlist of
business websites. This avoids search-engine blocks but still depends on each
site being reachable and allowing crawling:

```bash
python main.py --seeds data/seeds.example.txt --niche real_estate --max 10 --out data/leads.csv
```

`--seeds` takes a file with one URL per line (`#` comments and blank lines are
ignored), runs every URL through the exact same fetch → filter → extract →
export pipeline, and **bypasses search engines completely**. Pair it with any
method of finding websites (Google Maps, directories, your own research).

### CLI flags

| Flag              | Purpose                                              |
| ----------------- | ---------------------------------------------------- |
| `--niche X Y`     | Target one or more niches (default: all)             |
| `--max N`         | Max leads per niche (default: 20)                    |
| `--out FILE`      | Output path — `.csv` or `.json` (default: leads.csv) |
| `--json`          | Force JSON output regardless of `--out` extension    |
| `--seeds FILE`    | Scrape a URL list directly, bypass search engines    |
| `--no-enrich`     | Skip email MX validation + social discovery (faster) |
| `--workers N`     | Scrape N niches in parallel (default: 4)             |
| `--emails-only`   | Keep only leads that have at least one email         |
| `--min-quality N` | Drop leads scoring below N (0-100)                   |
| `--fresh`         | Ignore existing output — no merge, no domain skip    |
| `--no-merge`      | Overwrite `--out` with only this run's leads         |
| `--verbose`       | Debug logging                                        |
| `--dry-run`       | Validate config/extractors without scraping          |
| `--list-niches`   | Print available niches and exit                      |

### Local dashboard

```bash
python dashboard/manage_users.py init  # one-time setup: Tarun, Prabh, Uttkarsh
python dashboard/run.py --no-open
```

Open `http://127.0.0.1:8765/`. The dashboard binds to loopback only. Its run
and lead endpoints require one of the three accounts. Password hashes live in
`data/dashboard-users.json`, which is excluded from Git; the server refuses to
start until the accounts are provisioned. Rotate a password with
`python dashboard/manage_users.py set-password tarun` (or `prabh` / `uttkarsh`).
Rotation revokes active sessions. The dashboard uses a session cookie and
CSRF token for run controls. Keep it on loopback. Access from another machine
requires a separate secure deployment; do not expose the HTTP server directly.

The dashboard's run form accepts output files under `data/` or `output/` and seed files under
`data/`. Keep the CLI for output paths elsewhere. Choose **Search the web** to
discover sites by industry or **Use a seed list** to scrape known URLs. The
activity panel shows the current run and recovers its last status after a
dashboard restart. The library combines saved lead files, offers filters and
details, and shows published emails by default. **Published CSV** excludes
inferred addresses; **Export all** includes them with their `email_origin`
label. Inferred addresses are guesses, not verified mailboxes.

Discovery attempts to work with zero API keys using a failover chain
of free engines — Bing-RSS → DuckDuckGo → Bing → Mojeek → Brave → Ecosia →
SearXNG pool — automatically skipping any engine that bot-checks your IP.
Free engines can all block or return poor results from some networks; use
`--seeds` or configure SerpAPI when you need dependable discovery.
SerpAPI and ScrapingBee are optional upgrades auto-detected when their keys
are present.

## Zero-budget email harvesting tricks

- **Cloudflare email shields** are decoded (`data-cfemail`) — many sites hide
  their email behind Cloudflare and plain scrapers get nothing.
- **Encoded emails** (`info%40domain%2Ecom`) are unquoted.
- **Real contact links** are discovered from the page's own nav/footer text
  ("Contact us", "About", "Team") and crawled — not just guessed `/contact`.
- **JS-only sites** fall back to the free `r.jina.ai` reader proxy
  (`FREE_JS_RENDER=true`, no key) so emails hidden behind JavaScript still
  surface.
- **Search caching** (`CACHE_SEARCH=true`) keeps results in `data/cache/`
  for the configured cache window (one week by default).

## Incremental runs & lead quality

Runs are incremental by default:

- **Merge:** new leads are merged into the existing `--out` file instead of
  overwriting it (duplicate websites combine their distinct contacts).
- **Domain skip:** domains already in the output are skipped in search mode,
  so reruns find *new* prospects instead of re-scraping the same ones.
- Use `--fresh` to start from scratch or `--no-merge` to overwrite.

Every lead is rated with a **quality score** (0-100): emails weigh heaviest,
then WhatsApp → Instagram → LinkedIn → phones. Exports are sorted best-first
and each row is tagged `quality_label` (high / medium / low) so you prospect
the strongest contacts first.

## Supabase lead storage (optional)

The scraper can also upsert each successful run into a Supabase `public.leads`
table. This is persistence only; it does not move the local dashboard or its
authentication to Vercel.

1. In Supabase, open **SQL Editor → New query** and run
   [`supabase/schema.sql`](supabase/schema.sql).
2. Copy the Supabase **Project URL** and **Secret key** from
   **Project Settings → API Keys**.
3. Set these server-side environment variables (locally in `.env`, or in
   Vercel for a hosted deployment):

   ```dotenv
   SUPABASE_URL=https://your-project-ref.supabase.co
   SUPABASE_SECRET_KEY=sb_secret_...
   ```

4. Run the CLI normally. When both variables are present, the run writes the
   filtered leads to Supabase after the local export. Re-running a website
   upserts/refreshes its existing row rather than creating duplicates.

The secret key bypasses Row Level Security and must stay in a server environment;
never put it in `dashboard/app.js`, commit it, or send it through chat. No
Supabase Auth setup is required for lead persistence.

When deployed to Vercel, the handler accepts the platform's `*.vercel.app` host
and still requires a same-origin request. If you attach a custom domain, add
its hostname to the `DASHBOARD_ALLOWED_HOSTS` environment variable. The local
three-account credential file is intentionally not deployed. To enable the
same three usernames on Vercel, set `DASHBOARD_PASSWORD` in Vercel and, for
best practice, a separate `DASHBOARD_SESSION_SECRET` (the Supabase secret key
is used as a fallback). All three hosted usernames use the single
`DASHBOARD_PASSWORD` value. The password is never committed to Git.

The hosted Vercel page queues real scrape jobs in Supabase. The scheduled
GitHub Actions worker claims those jobs, runs the normal scraper (up to the
requested 500-lead limit), and upserts the actual results into Supabase. The
dashboard polls the durable job status and reads leads back from Supabase.

To enable the worker, add these GitHub Actions repository secrets:

- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY`
- Optional scraper keys: `SERPAPI_KEY`, `SCRAPINGBEE_API_KEY`, `PROXY_LIST`
- Optional tuning keys: `SEARCH_COUNTRY`, `REQUEST_DELAY_MIN`, `REQUEST_DELAY_MAX`,
  `TIMEOUT_SECONDS`, `MAX_CONCURRENT_REQUESTS`, `MAX_RETRIES`,
  `USER_AGENT_ROTATE`, `RESPECT_ROBOTS`, `VERIFY_EMAILS`, `INFER_EMAILS`,
  `FREE_JS_RENDER`, `CACHE_SEARCH`, `CACHE_SEARCH_HOURS`, `SEARX_INSTANCES`

The workflow runs every five minutes and can also be started manually from the
GitHub Actions tab. Vercel never runs the long-lived scraper process itself.

## API keys (optional)

- **[SerpAPI]** — `SERPAPI_KEY`: higher-quality Google organic results and
  bigger rate limits. Auto-used when set, else falls back to DuckDuckGo.
- **[ScrapingBee]** — `SCRAPINGBEE_API_KEY`: renders JS-heavy sites so
  contacts behind JavaScript are still parsed, and enables premium proxies
  automatically.
- **PROXY_LIST** (optional) — comma-separated
  `http://user:pass@host:port` entries used with rotation.

## Email verification

Scraped emails are checked for syntax and domain mail deliverability via the
`email-validator` library. Disposable email domains (mailinator, yopmail,
etc.) are filtered out. MX records do **not** verify that an individual
mailbox exists. Disable DNS checks with `VERIFY_EMAILS=false` in `.env` or
skip all enrichment with `--no-enrich`. Guessed generic addresses are disabled
by default; `INFER_EMAILS=true` enables them and marks them `inferred`.

## Social discovery

When a scraped page doesn't contain Instagram or LinkedIn links, the scraper
automatically searches DuckDuckGo for `site:instagram.com "domain"` and
`site:linkedin.com/company "domain"` to discover social profiles. This
requires no API keys — just the free search engines. Disable with `--no-enrich`.

## How it works

```
niche queries ──▶ DuckDuckGo / Bing-RSS / Bing / Mojeek (free) or SerpAPI ──▶ business URLs
        │                     or --seeds <url-list-file>
        ▼
fetch page (proxy-rotated, rate-limited, retry w/ backoff)
        │
        ▼
ScrapingBee render if empty/JS-only (optional)
        │
        ▼
URL deny-list → extract emails · mailto: links · wa.me numbers · @instagram · linkedin · phones
        │
        ▼
in-niche keyword filter + exclusion + corporate/portal/education rules
        │
        ▼
contact page crawl (/contact, /about, etc.) → merge emails
        │
        ▼
social discovery (IG + LinkedIn via DDG) → merge contacts
        │
        ▼
email MX validation (drop invalids + disposables)
        │
        ▼
dedupe (by website) → CSV / JSON export
```

## Project layout

```
config/   settings (delays, retries, proxy list) + niche definitions
core/     data model, extractors, networking, niche filter
sources/  search clients, page fetcher, collection pipeline, social/email enrichment
output/   CSV + JSON exporters + optional Supabase lead store
supabase/ SQL schema for the optional leads and job tables
scripts/  durable GitHub Actions scrape worker
utils/    proxy pool, host chunking helpers
```

## Extending

- **New niche:** add a `Niche` entry in `config/niches.py`.
- **New source:** subclass the pattern in `sources/collector.py`.

## Note

Target-site requests check robots.txt by default, including redirect targets.
Check each site's terms and use public business contact information responsibly.
Free search engines and the optional Jina reader are external services with
their own availability and usage limits.

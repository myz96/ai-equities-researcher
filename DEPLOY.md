# Deploy guide

The app is one container: FastAPI backend + static report UI, built from
`Dockerfile.web`.

## Environment variables

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Pays for the LLM calls. Required. |
| `MODEL_NAME` / `MODEL_PROVIDER` | Model for analyses. Use a frontier model at launch (e.g. `anthropic/claude-sonnet-5` + `OpenRouter`). |
| `DATA_PROVIDER` | `yfinance` (free, default) or `financialdatasets` (needs credits). |
| `FINANCIAL_DATASETS_API_KEY` | Only used when `DATA_PROVIDER=financialdatasets`. |
| `APP_PASSWORD` | Required — the site refuses all requests without it. Give the password to the friend. |
| `DATABASE_URL` | Required for durable storage (Neon Postgres). Without it the app falls back to ephemeral SQLite and loses all notes on every restart. |
| `SITE_NAME` / `SITE_TAGLINE` | Site branding. |
| `BIRTHDAY_MESSAGE` | Shows a banner on first visit. |

## Local test

```bash
docker build -f Dockerfile.web -t equity-analyst .
docker run -p 8000:8000 --env-file .env -e APP_PASSWORD=secret equity-analyst
```

Open http://localhost:8000 — log in with any username and the password.

## Render free tier (recommended)

Why not Vercel: Vercel runs serverless functions with execution time limits
and a 250 MB bundle limit. This app is a long-lived Python container that
streams for 2–4 minutes per report, with heavy dependencies. Render's free
tier runs the Docker container directly and streams without limits.

Steps (`render.yaml` in the repo root configures everything):

1. Push the repo to GitHub (private is fine).
2. Sign up at https://render.com with that GitHub account.
3. New → Blueprint → pick the repo. Render reads `render.yaml`.
4. Enter the two secrets when prompted: `OPENROUTER_API_KEY` and
   `APP_PASSWORD`.
5. Deploy. The site appears at `https://terliatian-capital.onrender.com`.

Free-tier caveat: the container sleeps after ~15 minutes without traffic.
The first visit after a sleep takes ~1 minute to wake. For a gift this is
acceptable; the $7/month plan removes it.

One analysis makes ~20 LLM calls and takes 2-4 minutes. The SSE stream sends
events continuously, so idle timeouts do not trigger.

## Notes

- All durable state (notes, personas, watchlist, bench) lives in Postgres via
  `DATABASE_URL`. The container disk is ephemeral and holds nothing of value.
- Schema changes: tables are created automatically on first boot
  (`create_all`), but existing tables are never altered — new columns need a
  manual `ALTER TABLE` against the database.
- A dropped connection does not stop a running committee session server-side;
  the spend completes but the note still saves.
- Keep the education-only disclaimer visible in the UI footer.

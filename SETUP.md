# Cloud Deployment — Step by Step

You'll spend ~45 min total. Order matters: Supabase first (needed by everything), then a local backfill test, then Render, then Netlify.

---

## 1. Supabase (10 min)

1. Go to <https://supabase.com>, click **Start your project**, sign up (GitHub login is easiest).
2. **New Project** → choose region **Mumbai (ap-south-1)** for lowest latency to NSE. Free tier is fine. Pick any database password (you won't use it).
3. Wait ~2 minutes for the project to provision.
4. Open **SQL Editor** (left sidebar) → **New query**. Paste the entire contents of [`db_schema/schema.sql`](db_schema/schema.sql) and click **Run**. You should see "Success. No rows returned."
5. Open **Project Settings → API**. Copy three values:
   - **Project URL** → `SUPABASE_URL`
   - **`anon` `public` key** → `SUPABASE_ANON_KEY` (safe to embed in frontend)
   - **`service_role` `secret` key** → `SUPABASE_SERVICE_KEY` (NEVER commit this; recorder writes use it)

## 2. Local test + backfill (5 min)

1. Copy [.env.example](.env.example) to `.env` in the project root.
2. Paste the three Supabase values you copied.
3. Install dependencies (one-time):
   ```powershell
   pip install -r requirements.txt
   ```
4. Sanity-check the connection:
   ```powershell
   python db.py
   ```
   Should print `snapshots count: 0` and `historical_candles count: 0`.
5. Backfill your local data (today's snapshots + 30-day Yahoo history):
   ```powershell
   python migrate_to_supabase.py
   ```
   You should see ~42 snapshot rows inserted + ~6,300 candles inserted (NIFTY 2106 + BANKNIFTY 2106 + VIX 2106).
6. Verify backtest reads from Supabase now:
   ```powershell
   python backtest.py --date 2026-05-20 --symbol NIFTY --horizon 30
   ```
   First line should say `(source: Supabase, N rows for NIFTY)` instead of JSONL.

If all that worked → your DB layer is solid. Time to deploy.

## 3. Push code to GitHub (5 min)

If you haven't already, create a repo and push:
```powershell
git init
git add .
git commit -m "Initial commit: cloud-ready signal recorder + dashboard"
# Create a new empty repo at github.com/yourusername/signals, then:
git remote add origin git@github.com:yourusername/signals.git
git branch -M main
git push -u origin main
```

Confirm `.env` and `frontend/config.js` are **not** in the commit (the `.gitignore` excludes them).

## 4. Render — recorder Cron Job (10 min)

> **⚠️ Phase 0 caveat:** NSE may throttle Render's US-based IPs. The first day on Render is also your test — if Supabase shows no rows after a few cron firings, see the **Fallback** section below.

1. Go to <https://render.com> → sign up with GitHub.
2. **New +** → **Blueprint**. Select your `signals` repo. Render reads [`render.yaml`](render.yaml) automatically.
3. Render will detect the `signal-recorder` cron service and ask for secret env vars (the ones marked `sync: false`):
   - `SUPABASE_URL` — paste from step 1
   - `SUPABASE_SERVICE_KEY` — paste from step 1
4. Click **Apply**. Render builds the image (~2 min).
5. **Test it now**: in the Render dashboard, open the `signal-recorder` cron, click **Trigger Run**. Watch the logs. You should see either:
   - `[HH:MM:SS] -> db+2 | NIFTY ... | BN ...` → success, snapshot written
   - `[HH:MM:SS] Market closed — skipping snapshot` → expected if you're outside market hours
   - `Supabase insert FAILED: ...` → check env vars
6. Open Supabase → Table Editor → `snapshots` → confirm the new row appeared (only if market was open).

## 5. Netlify — static dashboard (10 min)

1. Go to <https://netlify.com> → sign up with GitHub.
2. **Add new site → Import an existing project** → pick your `signals` repo.
3. Netlify auto-detects [`netlify.toml`](netlify.toml). Confirm:
   - **Build command**: from netlify.toml (writes config.js from env vars)
   - **Publish directory**: `frontend`
4. **Site settings → Environment variables** → add two vars:
   - `PUBLIC_SUPABASE_URL` — your Supabase project URL
   - `PUBLIC_SUPABASE_ANON_KEY` — your Supabase anon key (NOT the service key)
5. **Deploys → Trigger deploy → Deploy site**. Wait ~30 seconds.
6. Open the site URL Netlify gives you (e.g. `https://random-name-abc123.netlify.app`).
7. The dashboard should load and show your latest snapshot. If you're outside market hours, expect the "Market Closed" / "Stale" view — that's correct.

## 6. Cutover (2 min)

Once you've confirmed Render fires and Netlify renders, disable the local Windows scheduler so it doesn't double-write:

```powershell
Disable-ScheduledTask -TaskName 'NSE Signal Recorder'
```

Tomorrow morning at 09:15 IST, only Render runs the recorder. Both your laptop and your phone can hit the Netlify URL to see live signals.

---

## Fallback: NSE blocks Render IPs

If after 30 min of Render cron firings during market hours you see **0 new rows** in Supabase (or only NEUTRAL signals with `spot_price = 0`), NSE is rejecting Render's outbound IPs. Two options:

### Option A — Run recorder locally, push to Supabase

The current Windows scheduler ([run_recorder.bat](run_recorder.bat)) already supports Supabase writes (it imports `db.py`). Just:

1. Re-enable the Windows scheduler:
   ```powershell
   Enable-ScheduledTask -TaskName 'NSE Signal Recorder'
   ```
2. Pause / delete the Render cron in the Render dashboard so nothing duplicates.
3. Your laptop becomes the recorder; Supabase + Netlify still serve the cloud dashboard. Cost drops to $0/month.

### Option B — Use Indian-region VPS

Spin up a tiny Hostinger or DigitalOcean Bangalore droplet (~$4/month). Run the recorder there in a `cron` job. Supabase + Netlify unchanged.

---

## Cost summary

| Service | Free tier covers us? | Monthly cost |
|---|---|---|
| Supabase | ✅ 500 MB DB (decades of headroom) | ₹0 |
| Render Cron | ❌ Starter plan required | ~₹85 |
| Netlify | ✅ 100 GB bandwidth | ₹0 |
| **Total** | | **~₹85/month** |

Drop to ₹0 if you use Fallback A (local recorder).

## Troubleshooting

- **Netlify shows nothing / console error**: open browser dev tools, look for "SUPABASE_CONFIG missing". Check Netlify env vars are set, then redeploy.
- **Supabase says "permission denied for table snapshots"**: anon role tries to write. Re-run [`db_schema/schema.sql`](db_schema/schema.sql) to ensure RLS policies are in place.
- **Render cron runs but no row appears**: check logs for `Supabase insert FAILED`. Most common cause: wrong service key or extra whitespace in env var.
- **Local recorder errors with "module 'db' has no attribute 'is_configured'"**: re-pull latest code; this means an older `db.py` is in your Python path.

# APEX Pro v5 — Deploy to Railway

## Files needed
- main.py
- requirements.txt
- Procfile

## Step 1 — Create GitHub repo
1. Go to github.com → New repository → name it "apex-pro-v5"
2. Upload all 3 files (main.py, requirements.txt, Procfile)

## Step 2 — Deploy on Railway
1. Go to railway.app → New Project → Deploy from GitHub repo
2. Select your apex-pro-v5 repo
3. Railway auto-detects the Procfile and deploys

## Step 3 — Set Environment Variables
In Railway dashboard → Variables → Add:

  BYBIT_API_KEY      = your Bybit API key
  BYBIT_API_SECRET   = your Bybit API secret

Optional (for trade logging):
  SUPABASE_URL       = your Supabase project URL
  SUPABASE_KEY       = your Supabase anon key

## Step 4 — Bybit API Setup
1. Log in to Bybit → Account → API Management
2. Create new API key:
   - Name: APEX Pro v5
   - Permissions: Trade (read + write), Wallet (read)
   - IP whitelist: leave blank (Railway IPs change)
3. Copy API Key and Secret → paste into Railway variables

## Step 5 — Verify
Visit your Railway URL → you should see the dashboard
Check /api/health → should return {"ok": true, "status": "running"}

## Monitoring
Dashboard auto-refreshes every 30s
/api/status → full JSON status
/api/debug/balance → raw Bybit balance response

## What the bot does
- Scans SOL, XRP, DOGE, ADA, AVAX every 5 minutes
- Enters long when SATS Supertrend flips up AND TQI ≥ 0.6
- Places SL automatically with the order
- Monitors TP hit via price check
- Max 2 positions open at once
- Pauses for 24h if daily loss exceeds 10%

## Expected behaviour
- ~1 trade every 4-5 days (validated)
- 75% win rate (backtested, 90 days)
- Each trade risks ~$5.50 at $110 balance

# Server Migration Runbook — Singapore → allowed region

**Purpose:** relocate the always-on bot off the current **Singapore Vultr** box (Polymarket
geo-blocks SG — verified: `/api/geoblock` returns `{"blocked":true,"country":"SG"}`) to a fresh
Vultr instance in a **Polymarket-allowed region**, preserving every collected trade.

This is pure infrastructure relocation — **no code changes**. Total hands-on time ~30–45 min,
most of it waiting on `pip install`.

> Read-only Polymarket APIs (gamma, CLOB books) are NOT geofenced — that's why the simulation
> runs fine from Singapore. Only the **authenticated trade/onboarding path** is blocked. So the
> move is required *before going live*, not for the sim.

---

## 0. Pick a region (one-time decision)

Spin the new instance in a Vultr region whose country is NOT on Polymarket's block list.

- **Avoid (blocked):** Singapore, Tokyo/Osaka (JP), Sydney/Melbourne (AU), London (UK),
  Frankfurt (DE), Paris (FR), Warsaw (PL), Toronto (Ontario, CA).
- **Clean candidates:** Amsterdam (NL), Madrid (ES), Stockholm (SE), São Paulo (BR),
  Seoul (KR), Mumbai/Delhi/Bangalore (IN), Johannesburg (ZA), Mexico City (MX).

**Sizing:** match the current box — smallest plan with **≥1 GB RAM** (numpy/pandas/scipy
headroom), Ubuntu LTS. Reuse your SSH key at create time so first login works.

After the box is up, **before anything else**, confirm it's actually unblocked:

```bash
curl -s https://polymarket.com/api/geoblock      # want: {"blocked":false,...}
curl -s https://ipinfo.io/json                    # confirm country/region
```

If `blocked:true`, destroy it and pick a different region. Do not proceed.

---

## 1. Bootstrap the new server

SSH in as root, then:

```bash
# system deps
apt-get update && apt-get install -y python3 python3-venv python3-pip git curl build-essential
# Node 22 (matches current box: node v22, npm 9)
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y nodejs

# GitHub SSH key (so you can pull AND push from the new box)
ssh-keygen -t ed25519 -C "weatherbot-$(hostname)" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub      # add this to github.com/settings/keys (browser-copy if needed)
ssh -o StrictHostKeyChecking=accept-new -T git@github.com   # expect "successfully authenticated"

# clone
cd /root
git clone git@github.com:cameronandriesz2006-bot/weather_trading_bot.git
cd weather_trading_bot

# OR just run the helper that does the rest of this section:
bash migration/setup_new_server.sh
```

`migration/setup_new_server.sh` builds the venv, installs Python deps, and builds the frontend.
It deliberately does **NOT** create `.env`, copy the DB, or start the service — those are the
manual, sequenced steps below (secrets + the split-brain cutover).

---

## 2. Recreate `.env` (gitignored — does NOT travel via git)

Copy `migration/.env.template` to `.env` and fill in the real values. **Critical gotcha
(from CLAUDE.md):** pydantic-settings makes `.env` OVERRIDE `config.py`, so the live
`KELLY_FRACTION` must be set HERE.

```bash
cp migration/.env.template .env
nano .env       # set the real values — KELLY_FRACTION=0.05 etc.
```

Current production values to reproduce (verify against the live box before you stop it —
`sed 's/=.*/=.../' .env` on the old server to see the keys):

- `SIMULATION_MODE=True`   ← keep True until the live-execution build is done + gated
- `KELLY_FRACTION=0.05`    ← the halved value; MUST be in `.env`, not just config.py
- `INITIAL_BANKROLL=...`   ← match current
- `KALSHI_ENABLED=False`   ← unchanged (Kalshi deferred)

If `KALSHI_PRIVATE_KEY_PATH` points at a key file, `scp` that file over too.

---

## 3. THE CUTOVER (avoid two bots / split-brain DB)

Both servers write their own SQLite. If both run, the scoreboard splits. Sequence exactly:

```bash
# --- on the OLD (Singapore) box ---
systemctl stop weatherbot          # freezes the DB; open stakes aren't debited, safe to pause

# --- from your laptop (or scp directly box-to-box) ---
scp root@OLD_IP:/root/weather_trading_bot/tradingbot.db ./tradingbot.db
scp ./tradingbot.db root@NEW_IP:/root/weather_trading_bot/tradingbot.db
#   (29 MB — seconds)

# --- on the NEW box ---
cp migration/weatherbot.service /etc/systemd/system/weatherbot.service
systemctl daemon-reload
systemctl enable --now weatherbot
systemctl status weatherbot --no-pager
curl -s localhost:8000/api/stats | head    # sanity: bot answering, bankroll/trades intact
```

Then verify the dashboard over an SSH tunnel (control endpoints are unauthenticated — keep
the port private, never public):

```bash
ssh -L 8000:localhost:8000 root@NEW_IP     # then open http://localhost:8000
```

---

## 4. Decommission Singapore

Once the new box has run a full scan + you've confirmed trades/scoreboard are intact:

```bash
# on OLD box — leave stopped, then destroy the instance from the Vultr panel
systemctl disable weatherbot
```

Do NOT restart the old service. Two runners = diverging DBs.

---

## 5. Update local memory after cutover

The repo's `CLAUDE.md` and `memory/` say the live runner is the Singapore box. After the move,
update:

- `memory/server-is-live-runner.md` → new IP/region.
- `CLAUDE.md` "Current state" → note the relocation + reason (Polymarket SG geoblock).

---

## Rollback

Nothing is destroyed until step 4. If the new box misbehaves, just
`systemctl start weatherbot` on Singapore again (it still has the pre-cutover DB) and debug the
new box at leisure. The only data "lost" on rollback is any trade the new box placed after
cutover — copy its DB back if you want to keep those.

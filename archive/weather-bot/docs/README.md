# Prediction Market Trading Bot

A trading bot that identifies pricing inefficiencies in **daily high/low temperature** prediction markets. It builds its own **31-member ensemble weather forecast**, converts it into a probability for each market's temperature bucket, and trades when that differs from the market price by more than its edge threshold — on **Polymarket** (and **Kalshi**, when enabled). Features a React dashboard.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![React](https://img.shields.io/badge/react-18+-61DAFB) ![TypeScript](https://img.shields.io/badge/typescript-5.0+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

**100% free to run** - No paid APIs, no subscriptions. All data sources are free. Kalshi API key optional for Kalshi markets.

## Overview

### Strategy 1: BTC 5-Minute Up/Down
Scans Polymarket BTC 5-minute Up/Down markets every 60 seconds. Uses real-time 1-minute candle data from Coinbase/Kraken/Binance to compute RSI, momentum, VWAP deviation, SMA crossover, and market skew as a weighted composite signal. Trades when edge > 2%.

### Strategy 2: Weather Temperature (Kalshi + Polymarket)
Scans weather temperature markets on **Kalshi** (KXHIGH series) and **Polymarket** every 5 minutes. Uses 31-member GFS ensemble forecasts from Open-Meteo to estimate the probability of temperature thresholds being exceeded. Trades when edge > 8%. Kalshi markets are auto-discovered via the `KXHIGHNY`, `KXHIGHCHI`, `KXHIGHMIA`, `KXHIGHLAX`, `KXHIGHDEN` series tickers.

### Key Features

- **BTC Microstructure Analysis** - RSI, momentum (1m/5m/15m), VWAP, SMA crossover from real candle data
- **Ensemble Weather Forecasting** - 31-member GFS ensemble from Open-Meteo for probabilistic temperature predictions
- **Multi-Platform Trading** - Trades weather markets on both Kalshi (KXHIGH series) and Polymarket simultaneously
- **Edge Detection** - Identifies mispriced markets across both strategies and platforms
- **Kelly Criterion Sizing** - Fractional Kelly (15%) position sizing with per-trade caps
- **Signal Calibration** - Tracks predictions vs outcomes with Brier score
- **Professional Dashboard** - React 3-column dashboard with real-time updates
- **Simulation Mode** - Paper trading with virtual bankroll tracking and equity curves

## Quick Start

### 1. Backend Setup

```bash
cd kalshi-trading-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the backend
uvicorn backend.api.main:app --reload --port 8000
```

Backend will be at: http://localhost:8000
API docs at: http://localhost:8000/docs

### 2. Frontend Setup

```bash
cd frontend

# Install dependencies
npm install

# Run the frontend
npm run dev
```

Frontend will be at: http://localhost:5173

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                          FRONTEND                                │
│  React + TypeScript + TanStack Query + Tailwind                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐            │
│  │Indicators│ │ Weather  │ │ Signals  │ │  Trades  │            │
│  │  + Chart │ │  Panel   │ │  Table   │ │  Table   │            │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘            │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                          BACKEND                                 │
│  FastAPI + Python + SQLite + APScheduler                         │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐        │
│  │  BTC      │ │ Weather   │ │  Signal   │ │Settlement │        │
│  │ Signals   │ │ Signals   │ │ Scheduler │ │  Engine   │        │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘        │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  │Coinbase/ │ │Open-Meteo│ │  NWS     │ │Polymarket│ │ Kalshi │ │
│  │Kraken/   │ │ Ensemble │ │  API     │ │Gamma API │ │  API   │ │
│  │Binance   │ │  (GFS)   │ │          │ │          │ │(KXHIGH)│ │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## How It Works

### BTC 5-Minute Strategy
1. Fetch 60 one-minute candles from Coinbase/Kraken/Binance (fallback chain)
2. Compute 5 indicators: RSI(14), Momentum(1m/5m/15m), VWAP deviation, SMA crossover, Market skew
3. Convergence filter: require 2+ of 4 indicators to agree
4. Weighted composite -> model UP probability (0.35-0.65 range)
5. Compare to Polymarket prices, trade the side with higher edge

### Weather Temperature Strategy
1. Fetch open weather markets from Kalshi (KXHIGH series, RSA-PSS auth) and Polymarket (Gamma API)
2. Fetch 31-member GFS ensemble forecasts from Open-Meteo
3. Count fraction of members above/below the market's temperature threshold
4. That fraction = model probability (e.g., 28/31 members above 70F = 90% probability)
5. Compare to market price on either platform, trade when edge > 8%
6. Confidence = ensemble agreement (how one-sided the 31 members are)

### Edge Calculation
```
edge = model_probability - market_probability
```
BTC signals require |edge| > 2%. Weather signals require |edge| > 8%.

### Position Sizing (Fractional Kelly)
```
kelly = (win_prob * odds - lose_prob) / odds
position_size = kelly * 0.15 * bankroll
```
Capped at 5% of bankroll and $75 (BTC) or $100 (Weather) per trade.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/dashboard` | GET | All dashboard data in one call |
| `/api/btc/price` | GET | Current BTC price + momentum |
| `/api/btc/windows` | GET | Active BTC 5-min windows |
| `/api/signals` | GET | Current BTC trading signals |
| `/api/signals/actionable` | GET | BTC signals above threshold |
| `/api/kalshi/status` | GET | Kalshi API auth status + balance |
| `/api/weather/forecasts` | GET | Ensemble forecasts for all cities |
| `/api/weather/markets` | GET | Weather markets (Kalshi + Polymarket) |
| `/api/weather/signals` | GET | Weather trading signals (both platforms) |
| `/api/trades` | GET | Trade history |
| `/api/stats` | GET | Bot statistics |
| `/api/calibration` | GET | Signal calibration data |
| `/api/run-scan` | POST | Trigger BTC + weather scan |
| `/api/simulate-trade` | POST | Simulate a BTC trade |
| `/api/settle-trades` | POST | Check settlements |
| `/api/bot/start` | POST | Start trading |
| `/api/bot/stop` | POST | Pause trading |
| `/api/bot/reset` | POST | Reset all trades |
| `/api/events` | GET | Event log |
| `/ws/events` | WS | Real-time event stream |

## Configuration

All settings in `backend/config.py`, overridable via environment variables:

### BTC Settings
| Setting | Default | Description |
|---------|---------|-------------|
| `SCAN_INTERVAL_SECONDS` | 60 | BTC scan frequency |
| `MIN_EDGE_THRESHOLD` | 0.02 | Minimum edge (2%) |
| `MAX_ENTRY_PRICE` | 0.55 | Max entry price (55c) |
| `KELLY_FRACTION` | 0.10 | Fractional Kelly multiplier |
| `KELLY_MAX_TRADE_FRACTION` | 0.05 | Max stake on a single bet, as a fraction of the live bankroll (5%) |

### Kalshi Settings
| Setting | Default | Description |
|---------|---------|-------------|
| `KALSHI_API_KEY_ID` | None | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | None | Path to RSA private key PEM file |
| `KALSHI_ENABLED` | True | Enable/disable Kalshi market fetching |

### Weather Settings
| Setting | Default | Description |
|---------|---------|-------------|
| `WEATHER_ENABLED` | True | Enable/disable weather trading |
| `WEATHER_SCAN_INTERVAL_SECONDS` | 300 | Weather scan frequency (5 min) |
| `WEATHER_MIN_EDGE_THRESHOLD` | 0.08 | Minimum edge (8%) |
| `WEATHER_MAX_ENTRY_PRICE` | 0.70 | Max entry price (70c) |
| `WEATHER_MAX_ALLOCATION_FRACTION` | 0.20 | Max open weather exposure, as a fraction of bankroll (20%) |
| `WEATHER_MIN_TRADE_FRACTION` | 0.001 | Minimum stake per trade, as a fraction of bankroll (0.1%) |
| `WEATHER_CITIES` | nyc,chicago,miami,los_angeles,denver,london,tokyo,seoul,paris,shanghai,hong_kong | Cities to track |

### Risk Management
| Setting | Default | Description |
|---------|---------|-------------|
| `DAILY_LOSS_LIMIT_FRACTION` | 0.075 | Daily loss circuit breaker, as a fraction of bankroll (7.5%) |
| `MAX_TOTAL_PENDING_TRADES` | 20 | Max open positions |
| `INITIAL_BANKROLL` | 10000.0 | Starting paper bankroll |

## Supported Cities (Weather)

| City | Station | Tracked |
|------|---------|---------|
| New York | KNYC | Default |
| Chicago | KORD | Default |
| Miami | KMIA | Default |
| Los Angeles | KLAX | Default |
| Denver | KDEN | Default |

Add more cities by editing `WEATHER_CITIES` in config and adding entries to `CITY_CONFIG` in `backend/data/weather.py`.

## Data Sources

| Source | Data | Used For | Auth |
|--------|------|----------|------|
| Coinbase | BTC 1-min candles | BTC microstructure | None |
| Kraken | BTC 1-min candles | BTC fallback | None |
| Binance | BTC 1-min candles | BTC fallback | None |
| Open-Meteo | GFS Ensemble (31 members) | Weather probability | None |
| NWS API | Observed temperatures | Weather settlement | None |
| Polymarket | Market prices + resolution | Both strategies | None |
| Kalshi | Weather temperature markets (KXHIGH) | Weather strategy | RSA key |

## Project Structure

```
kalshi-trading-bot/
├── backend/
│   ├── api/
│   │   └── main.py                 # FastAPI routes + dashboard
│   ├── core/
│   │   ├── signals.py              # BTC signal generation
│   │   ├── weather_signals.py      # Weather signal generation
│   │   ├── scheduler.py            # Background jobs (BTC + weather)
│   │   └── settlement.py           # Trade settlement (routes by market_type)
│   ├── data/
│   │   ├── btc_markets.py          # Polymarket BTC market fetcher
│   │   ├── crypto.py               # BTC price + microstructure
│   │   ├── kalshi_client.py        # Kalshi API client (RSA-PSS auth)
│   │   ├── kalshi_markets.py       # Kalshi weather market fetcher (KXHIGH)
│   │   ├── weather.py              # Open-Meteo ensemble + NWS observations
│   │   ├── weather_markets.py      # Polymarket weather market fetcher
│   │   └── markets.py              # Generic market wrapper
│   ├── models/
│   │   └── database.py             # SQLAlchemy models (market_type column)
│   └── config.py                   # All settings (BTC + weather)
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── GlobeView.tsx        # 3D globe with city markers
│   │   │   ├── EdgeDistribution.tsx # Edge distribution chart
│   │   │   ├── MicrostructurePanel.tsx # RSI gauge + indicator meters
│   │   │   ├── WeatherPanel.tsx     # Weather forecasts per city
│   │   │   ├── CalibrationPanel.tsx # Prediction accuracy tracking
│   │   │   ├── StatsCards.tsx       # Performance metrics
│   │   │   ├── SignalsTable.tsx     # BTC + Weather signals combined
│   │   │   ├── TradesTable.tsx      # Trade history
│   │   │   ├── EquityChart.tsx      # P&L chart
│   │   │   └── Terminal.tsx         # Event log + controls
│   │   ├── App.tsx                  # 3-column grid dashboard
│   │   ├── api.ts                   # API client
│   │   └── types.ts                 # TypeScript interfaces
│   └── package.json
├── requirements.txt
├── run.py
└── README.md
```

## Disclaimer

This is a **simulation tool** for educational purposes. It does not place real trades or use real money. Past performance in simulation does not guarantee future results. Prediction markets involve risk of loss.

## License

MIT - do whatever you want with it.

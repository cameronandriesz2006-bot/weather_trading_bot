export interface Trade {
  id: number
  market_ticker: string
  platform: string
  event_slug?: string | null
  direction: string
  entry_price: number
  size: number
  timestamp: string
  settled: boolean
  result: string
  pnl: number | null
  // Readable market identity + settlement info
  bucket_label?: string | null
  city_name?: string | null
  metric?: string | null
  target_date?: string | null
  settlement_time?: string | null
  market_type?: string | null
  current_price?: number | null
  unrealized_pnl?: number | null
  bias_corrected?: boolean | null
}

export interface BotStats {
  bankroll: number
  total_trades: number
  winning_trades: number
  win_rate: number
  total_pnl: number
  is_running: boolean
  last_run: string | null
  settled_trades?: number
  weather_max_allocation?: number
  daily_loss_limit?: number
  daily_pnl?: number
}

export interface EquityPoint {
  timestamp: string
  pnl: number
  bankroll: number
}

export interface CalibrationSummary {
  total_signals: number
  total_with_outcome: number
  accuracy: number
  avg_predicted_edge: number
  avg_actual_edge: number
  brier_score: number
}

export interface BiasSegment {
  label: string            // "corrected" | "uncorrected"
  open_trades: number
  open_exposure: number
  settled: number
  wins: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
  brier_score: number | null
}

export interface WeatherForecast {
  city_key: string
  city_name: string
  target_date: string
  mean_high: number
  std_high: number
  mean_low: number
  std_low: number
  num_members: number
  ensemble_agreement: number
}

export interface WeatherSignal {
  market_id: string
  city_key: string
  city_name: string
  target_date: string
  threshold_f: number
  metric: string
  direction: string
  model_probability: number
  market_probability: number
  edge: number
  confidence: number
  suggested_size: number
  reasoning: string
  ensemble_mean: number
  ensemble_std: number
  ensemble_members: number
  actionable: boolean
  platform?: string
  // Dashboard redesign: exact market identity + cost-aware economics
  slug?: string
  bucket_label?: string
  unit?: string            // "F" (US) or "C" (international)
  low_f?: number | null
  high_f?: number | null
  net_edge?: number
  entry_price?: number
  cost?: number
  rel_spread?: number
  liquidity?: number
  spread?: number
  yes_price?: number
  no_price?: number
  bias?: number
}

export interface EventLogEntry {
  timestamp: string
  type: 'info' | 'success' | 'warning' | 'error' | 'data' | 'trade' | 'heartbeat'
  message: string
  data?: Record<string, unknown>
}

export interface WorkingOrder {
  order_id: string
  city_name?: string | null
  metric?: string | null
  bucket_label?: string | null
  target_date?: string | null
  direction: string                 // side we're buying: "yes" / "no"
  limit_price: number
  size_shares: number
  intended_cash: number
  status: string                    // OPEN / PARTIALLY_FILLED / FILLED / EXPIRED / CANCELLED
  filled_shares: number
  avg_fill_price?: number | null
  fill_pct: number                  // filled / requested (0-1)
  created_at?: string | null
  expires_in_seconds?: number | null
}

export interface DashboardData {
  stats: BotStats
  recent_trades: Trade[]
  equity_curve: EquityPoint[]
  calibration: CalibrationSummary | null
  bias_segments?: BiasSegment[]
  city_segments?: BiasSegment[]   // active (still traded) vs retired cities (no longer rendered)
  working_orders?: WorkingOrder[]   // resting maker limit orders
  weather_signals: WeatherSignal[]
  weather_forecasts: WeatherForecast[]
  scoreboard_epoch?: string | null   // UTC ISO cutoff the scoreboard is scored from
}

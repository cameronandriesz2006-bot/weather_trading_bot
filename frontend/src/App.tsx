import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchDashboard, runScan, startBot, stopBot } from './api'
import { ScanView } from './components/ScanView'
import { TradesPanel } from './components/TradesPanel'
import { Scoreboard } from './components/Scoreboard'
import { LiveLog } from './components/LiveLog'

function LiveClock() {
  const [time, setTime] = useState(new Date())
  useEffect(() => {
    const interval = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(interval)
  }, [])
  return (
    <span className="text-xs tabular-nums text-neutral-400">
      {time.toLocaleTimeString('en-US', { hour12: false })}
    </span>
  )
}

function StatCard({ label, value, sub, tone = 'neutral' }: {
  label: string; value: string; sub?: string; tone?: 'pos' | 'neg' | 'neutral'
}) {
  const color = tone === 'pos' ? 'text-green-400' : tone === 'neg' ? 'text-red-400' : 'text-neutral-100'
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 px-4 py-3 min-w-[140px]">
      <div className="text-[11px] uppercase tracking-wider text-neutral-500">{label}</div>
      <div className={`text-xl font-semibold tabular-nums mt-1 ${color}`}>{value}</div>
      {sub && <div className="text-xs text-neutral-500 mt-0.5">{sub}</div>}
    </div>
  )
}

function App() {
  const queryClient = useQueryClient()

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard'],
    queryFn: fetchDashboard,
    refetchInterval: 10000,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  const scanMutation = useMutation({ mutationFn: runScan, onSuccess: invalidate })
  const startMutation = useMutation({ mutationFn: startBot, onSuccess: invalidate })
  const stopMutation = useMutation({ mutationFn: stopBot, onSuccess: invalidate })

  const weatherSignals = data?.weather_signals ?? []
  const recentTrades = data?.recent_trades ?? []
  const calibration = data?.calibration ?? null
  const biasSegments = data?.bias_segments ?? []
  const citySegments = data?.city_segments ?? []
  const scoreboardEpoch = data?.scoreboard_epoch ?? null
  const stats = data?.stats ?? {
    is_running: false, last_run: null, total_trades: 0, total_pnl: 0,
    bankroll: 10000, winning_trades: 0, win_rate: 0,
  }
  const allocationCap = stats.weather_max_allocation ?? 2000
  const dailyLossLimit = stats.daily_loss_limit ?? 750
  // daily_pnl is today's realized P&L; loss used toward the breaker is its
  // negative part (the breaker trips at -dailyLossLimit).
  const dailyPnl = stats.daily_pnl ?? 0
  const dailyLossUsed = dailyPnl < 0 ? -dailyPnl : 0
  const breakerHit = dailyLossUsed >= dailyLossLimit

  const openTrades = recentTrades.filter(t => !t.settled)
  const openExposure = openTrades.reduce((a, t) => a + (t.size || 0), 0)
  // Map of market_id -> open position, so the scan view can mark buckets we hold.
  const heldByMarket = openTrades.reduce((m, t) => {
    m[t.market_ticker] = { direction: t.direction, entry_price: t.entry_price }
    return m
  }, {} as Record<string, { direction: string; entry_price: number }>)
  // "Actionable now" = opportunities we could still act on — exclude markets we already hold.
  const actionableCount = weatherSignals.filter(s => s.actionable && !heldByMarket[s.market_id]).length
  // win_rate may arrive as a fraction (0-1) or a percent; normalise.
  const winRatePct = stats.win_rate <= 1 ? stats.win_rate * 100 : stats.win_rate

  if (isLoading) {
    return (
      <div className="min-h-screen bg-neutral-950 flex items-center justify-center">
        <div className="text-center">
          <div className="relative w-10 h-10 mx-auto mb-4">
            <div className="absolute inset-0 border-2 border-neutral-800 rounded-full" />
            <div className="absolute inset-0 border-2 border-transparent border-t-green-500 rounded-full animate-spin" />
          </div>
          <div className="text-[11px] text-neutral-500 uppercase tracking-widest">Loading</div>
        </div>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="min-h-screen bg-neutral-950 flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-500 text-sm uppercase mb-3 tracking-wider">Connection error</div>
          <div className="text-neutral-500 text-xs mb-4">Is the backend running? (python run.py)</div>
          <button
            onClick={() => refetch()}
            className="px-4 py-2 bg-neutral-900 border border-neutral-700 hover:border-neutral-500 text-neutral-300 text-xs uppercase tracking-wider rounded"
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-200">
      {/* ===== Header ===== */}
      <header className="sticky top-0 z-20 border-b border-neutral-800 bg-neutral-950/90 backdrop-blur px-6 py-3 flex items-center gap-3">
        <h1 className="text-base font-semibold text-neutral-100">Weather Markets</h1>
        <span className="px-2 py-0.5 text-[10px] font-bold uppercase rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">Sim</span>
        <span className={`px-2 py-0.5 text-[10px] font-bold uppercase rounded ${
          stats.is_running ? 'bg-green-500/10 text-green-400 border border-green-500/20' : 'bg-neutral-800 text-neutral-400 border border-neutral-700'
        }`}>
          {stats.is_running ? 'Live' : 'Idle'}
        </span>
        <div className="flex-1" />
        <button
          onClick={() => (stats.is_running ? stopMutation.mutate() : startMutation.mutate())}
          className="px-3 py-1.5 text-xs rounded border border-neutral-700 hover:border-neutral-500 text-neutral-200"
        >
          {stats.is_running ? 'Stop' : 'Start'}
        </button>
        <button
          onClick={() => scanMutation.mutate()}
          disabled={scanMutation.isPending}
          className="px-3 py-1.5 text-xs rounded border border-neutral-700 hover:border-neutral-500 text-neutral-200 disabled:opacity-50"
        >
          {scanMutation.isPending ? 'Scanning…' : 'Scan now'}
        </button>
        <LiveClock />
      </header>

      <main className="max-w-6xl mx-auto px-6 py-6 space-y-8">
        {/* ===== Status bar ===== */}
        <div className="flex flex-wrap gap-3">
          <StatCard label="Bankroll" value={`$${stats.bankroll.toLocaleString(undefined, { maximumFractionDigits: 0 })}`} />
          <StatCard label="Total P&L" value={`${stats.total_pnl >= 0 ? '+' : ''}$${stats.total_pnl.toFixed(0)}`} tone={stats.total_pnl >= 0 ? 'pos' : 'neg'} />
          <StatCard label="Win rate" value={`${winRatePct.toFixed(0)}%`} sub={`${stats.winning_trades}/${stats.settled_trades ?? 0} settled`} />
          <StatCard label="Open positions" value={`${openTrades.length}`} sub={`$${openExposure.toFixed(0)} / $${allocationCap.toLocaleString(undefined, { maximumFractionDigits: 0 })} cap`} />
          <StatCard
            label="Daily loss"
            value={`$${dailyLossUsed.toFixed(0)} / $${dailyLossLimit.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
            sub={breakerHit ? 'limit hit — trading paused' : `today's P&L ${dailyPnl >= 0 ? '+' : ''}$${dailyPnl.toFixed(0)}`}
            tone={breakerHit ? 'neg' : dailyLossUsed > 0 ? 'neg' : 'neutral'}
          />
          <StatCard label="Actionable now" value={`${actionableCount}`} sub={`of ${weatherSignals.length} scanned`} />
        </div>

        {/* ===== Scan view (hero) ===== */}
        <ScanView signals={weatherSignals} held={heldByMarket} />

        {/* ===== Positions & trades ===== */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2">
            Positions &amp; trades
          </h2>
          <div className="rounded-xl border border-neutral-800 bg-neutral-900/40 overflow-hidden">
            <TradesPanel trades={recentTrades} />
          </div>
        </section>

        {/* ===== Scoreboard & calibration ===== */}
        <Scoreboard calibration={calibration} biasSegments={biasSegments} citySegments={citySegments} epoch={scoreboardEpoch} />

        {/* ===== Live event log (collapsible) ===== */}
        <LiveLog />
      </main>
    </div>
  )
}

export default App

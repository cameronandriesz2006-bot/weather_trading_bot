import { useMemo, useState } from 'react'
import { formatDistanceToNow } from 'date-fns'
import type { Trade } from '../types'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function fmtDate(iso?: string | null): string {
  if (!iso) return ''
  const [, m, d] = iso.split('-').map(Number)
  if (!m || !d) return iso
  return `${MONTHS[m - 1]} ${d}`
}

function metricLabel(m?: string | null): string {
  return m === 'high' ? 'High' : m === 'low' ? 'Low' : ''
}

function marketLabel(t: Trade): string {
  if (t.city_name && t.target_date) {
    const base = `${t.city_name} · ${metricLabel(t.metric)} · ${fmtDate(t.target_date)}`
    return t.bucket_label ? `${base} · ${t.bucket_label}` : base
  }
  return t.event_slug || t.market_ticker
}

// Countdown to the end of the target local day (when the high/low is fixed).
function settlesIn(targetDate?: string | null): string {
  if (!targetDate) return '—'
  const [y, m, d] = targetDate.split('-').map(Number)
  if (!y || !m || !d) return '—'
  const settleAt = new Date(y, m - 1, d + 1).getTime() // local midnight after the target day
  const ms = settleAt - Date.now()
  if (ms <= 0) return 'awaiting resolution'
  const mins = Math.floor(ms / 60000)
  const days = Math.floor(mins / 1440)
  const hrs = Math.floor((mins % 1440) / 60)
  const rem = mins % 60
  if (days > 0) return `${days}d ${hrs}h`
  if (hrs > 0) return `${hrs}h ${rem}m`
  return `${rem}m`
}

function isLong(dir: string): boolean {
  return dir === 'yes' || dir === 'up' || dir === 'above'
}

// Backend sends UTC timestamps without a tz marker; tag them so the browser
// reads them as UTC (otherwise "X ago" is off by the local UTC offset).
function asUtc(ts: string): string {
  return /[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : ts + 'Z'
}

const POLY = 'https://polymarket.com/event/'

function MarketCell({ t }: { t: Trade }) {
  const label = marketLabel(t)
  if (t.event_slug) {
    return (
      <a
        href={`${POLY}${t.event_slug}`}
        target="_blank"
        rel="noopener noreferrer"
        className="text-neutral-200 hover:text-blue-300"
      >
        {label} <span className="text-blue-400">↗</span>
      </a>
    )
  }
  return <span className="text-neutral-200">{label}</span>
}

function SideCell({ dir }: { dir: string }) {
  return <span className={isLong(dir) ? 'text-green-400 font-medium' : 'text-red-400 font-medium'}>{dir.toUpperCase()}</span>
}

export function TradesPanel({ trades }: { trades: Trade[] }) {
  const active = useMemo(() => trades.filter(t => !t.settled), [trades])
  const settled = useMemo(() => trades.filter(t => t.settled), [trades])
  const [view, setView] = useState<'active' | 'settled'>('active')
  const rows = view === 'active' ? active : settled

  return (
    <div>
      <div className="flex items-center gap-3 px-4 py-3 border-b border-neutral-800">
        <div className="relative inline-block">
          <select
            value={view}
            onChange={e => setView(e.target.value as 'active' | 'settled')}
            className="appearance-none bg-neutral-900 border border-neutral-700 rounded-md pl-3 pr-9 py-2 text-sm text-neutral-100 focus:outline-none focus:border-neutral-500 cursor-pointer"
          >
            <option value="active" className="bg-neutral-900 text-neutral-100">Active — not settled ({active.length})</option>
            <option value="settled" className="bg-neutral-900 text-neutral-100">Settled ({settled.length})</option>
          </select>
          <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-neutral-400 text-xs">▾</span>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="py-10 text-center text-sm text-neutral-500">
          {view === 'active' ? 'No active positions.' : 'No settled trades yet.'}
        </div>
      ) : view === 'active' ? (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-neutral-500 text-xs uppercase tracking-wider border-b border-neutral-800">
              <th className="text-left font-medium px-4 py-2.5">Market</th>
              <th className="text-center font-medium px-3 py-2.5">Side</th>
              <th className="text-right font-medium px-3 py-2.5">Entry</th>
              <th className="text-right font-medium px-3 py-2.5">Now</th>
              <th className="text-right font-medium px-3 py-2.5">Size</th>
              <th className="text-right font-medium px-3 py-2.5">Unrealized</th>
              <th className="text-right font-medium px-4 py-2.5">Settles in</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(t => {
              const hasMtm = t.current_price != null
              const up = (t.unrealized_pnl ?? 0) >= 0
              const pct = hasMtm && t.entry_price ? (t.current_price! - t.entry_price) / t.entry_price * 100 : 0
              return (
                <tr key={t.id} className="border-b border-neutral-800/60">
                  <td className="px-4 py-2.5"><MarketCell t={t} /></td>
                  <td className="px-3 py-2.5 text-center"><SideCell dir={t.direction} /></td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-neutral-300">{(t.entry_price * 100).toFixed(0)}¢</td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-neutral-300">{hasMtm ? `${(t.current_price! * 100).toFixed(0)}¢` : '—'}</td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-neutral-300">${t.size.toFixed(0)}</td>
                  <td className={`px-3 py-2.5 text-right tabular-nums ${hasMtm ? (up ? 'text-green-400' : 'text-red-400') : 'text-neutral-500'}`}>
                    {hasMtm ? `${up ? '+' : ''}$${(t.unrealized_pnl ?? 0).toFixed(2)} (${up ? '+' : ''}${pct.toFixed(0)}%)` : '—'}
                  </td>
                  <td className="px-4 py-2.5 text-right tabular-nums text-neutral-400">{settlesIn(t.target_date)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-neutral-500 text-xs uppercase tracking-wider border-b border-neutral-800">
              <th className="text-left font-medium px-4 py-2.5">Market</th>
              <th className="text-center font-medium px-3 py-2.5">Side</th>
              <th className="text-right font-medium px-3 py-2.5">Entry</th>
              <th className="text-right font-medium px-3 py-2.5">Size</th>
              <th className="text-center font-medium px-3 py-2.5">Result</th>
              <th className="text-right font-medium px-3 py-2.5">P&amp;L</th>
              <th className="text-right font-medium px-4 py-2.5">Settled</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(t => {
              const win = t.result === 'win'
              const loss = t.result === 'loss'
              return (
                <tr key={t.id} className="border-b border-neutral-800/60">
                  <td className="px-4 py-2.5"><MarketCell t={t} /></td>
                  <td className="px-3 py-2.5 text-center"><SideCell dir={t.direction} /></td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-neutral-300">{(t.entry_price * 100).toFixed(0)}¢</td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-neutral-300">${t.size.toFixed(0)}</td>
                  <td className="px-3 py-2.5 text-center">
                    <span className={`text-xs font-semibold uppercase ${win ? 'text-green-400' : loss ? 'text-red-400' : 'text-neutral-400'}`}>{t.result}</span>
                  </td>
                  <td className={`px-3 py-2.5 text-right tabular-nums font-semibold ${(t.pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {t.pnl != null ? `${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}` : '—'}
                  </td>
                  <td className="px-4 py-2.5 text-right text-neutral-500 text-xs">
                    {t.settlement_time ? `${formatDistanceToNow(new Date(asUtc(t.settlement_time)))} ago` : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}

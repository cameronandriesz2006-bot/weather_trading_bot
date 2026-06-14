import { useMemo, useState } from 'react'
import type { WeatherSignal } from '../types'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function fmtDate(iso: string): string {
  const [, m, d] = iso.split('-').map(Number)
  if (!m || !d) return iso
  return `${MONTHS[m - 1]} ${d}`
}

function pct(x: number): string {
  return `${(x * 100).toFixed(0)}%`
}

function signedPct(x: number): string {
  return `${x >= 0 ? '+' : ''}${(x * 100).toFixed(1)}%`
}

function metricLabel(metric: string): string {
  return metric === 'high' ? 'High' : 'Low'
}

interface EventGroup {
  slug: string
  cityName: string
  metric: string
  targetDate: string
  ensembleMean: number
  ensembleStd: number
  members: number
  bias: number
  unit: string
  signals: WeatherSignal[]
  actionableCount: number
  heldCount: number
}

// Natural ladder order: "X or below" first, then ranges ascending, "X or higher" last.
function bucketSortKey(s: WeatherSignal): number {
  if (s.low_f == null) return -Infinity
  return s.low_f
}

// Pull the bracketed reason out of "[FILTERED] [reasons...] City ..." for display.
function filterReason(s: WeatherSignal): string {
  const m = s.reasoning.match(/\[(?:FILTERED|ACTIONABLE)\]\s*\[([^\]]+)\]/)
  return m ? m[1] : 'filtered'
}

const POLY_EVENT_URL = 'https://polymarket.com/event/'

interface ScanViewProps {
  signals: WeatherSignal[]
  /** market_id -> open position we currently hold (to distinguish from signals) */
  held?: Record<string, { direction: string; entry_price: number }>
}

export function ScanView({ signals, held = {} }: ScanViewProps) {
  const events = useMemo<EventGroup[]>(() => {
    const map = new Map<string, EventGroup>()
    for (const s of signals) {
      const slug = s.slug || `${s.city_key}-${s.metric}-${s.target_date}`
      let g = map.get(slug)
      if (!g) {
        g = {
          slug,
          cityName: s.city_name,
          metric: s.metric,
          targetDate: s.target_date,
          ensembleMean: s.ensemble_mean,
          ensembleStd: s.ensemble_std,
          members: s.ensemble_members,
          bias: s.bias ?? 0,
          unit: s.unit ?? 'F',
          signals: [],
          actionableCount: 0,
          heldCount: 0,
        }
        map.set(slug, g)
      }
      g.signals.push(s)
      // Count only actionable opportunities we don't already hold.
      if (s.actionable && !held[s.market_id]) g.actionableCount++
      // Count buckets in this event we currently hold an open position in.
      if (held[s.market_id]) g.heldCount++
    }
    const arr = Array.from(map.values())
    arr.forEach(g => g.signals.sort((a, b) => bucketSortKey(a) - bucketSortKey(b)))
    arr.sort((a, b) => b.actionableCount - a.actionableCount || b.heldCount - a.heldCount || a.cityName.localeCompare(b.cityName))
    return arr
  }, [signals, held])

  const [selectedSlug, setSelectedSlug] = useState<string | null>(null)
  const selected = useMemo(() => {
    if (events.length === 0) return null
    return events.find(e => e.slug === selectedSlug) ?? events[0]
  }, [events, selectedSlug])

  const opportunities = useMemo(
    () => signals
      .filter(s => s.actionable && !held[s.market_id])   // exclude markets we already hold
      .sort((a, b) => (b.net_edge ?? 0) - (a.net_edge ?? 0))
      .slice(0, 8),
    [signals, held],
  )

  if (signals.length === 0) {
    return (
      <div className="text-neutral-500 text-sm py-12 text-center">
        No weather markets scanned yet. Click “Scan now” or wait for the next cycle.
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* ===== Top opportunities ===== */}
      {opportunities.length > 0 && (
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2">
            Top opportunities ({opportunities.length})
          </h2>
          <div className="flex gap-3 overflow-x-auto pb-1">
            {opportunities.map(s => (
              <button
                key={s.market_id}
                onClick={() => s.slug && setSelectedSlug(s.slug)}
                className="shrink-0 text-left rounded-lg border border-green-500/30 bg-green-500/5 hover:bg-green-500/10 px-4 py-3 min-w-[210px] transition-colors"
              >
                <div className="text-sm font-medium text-neutral-100">{s.city_name} · {s.bucket_label}</div>
                <div className="text-xs text-neutral-400 mt-0.5">{metricLabel(s.metric)} · {fmtDate(s.target_date)}</div>
                <div className="mt-2 flex items-baseline gap-2">
                  <span className="text-green-400 font-semibold">BUY {(s.direction || '').toUpperCase()}</span>
                  <span className="text-neutral-400 text-xs">@ {((s.entry_price ?? 0) * 100).toFixed(0)}¢</span>
                </div>
                <div className="text-xs text-neutral-400 mt-1">
                  net edge <span className="text-green-400 font-medium">{signedPct(s.net_edge ?? 0)}</span> · ${(s.suggested_size ?? 0).toFixed(0)}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ===== Scanned markets (dropdown -> one event) ===== */}
      <div>
        <div className="flex flex-wrap items-center gap-3 mb-3">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-neutral-400">Scanned markets</h2>
          <div className="relative inline-block">
            <select
              value={selected?.slug ?? ''}
              onChange={e => setSelectedSlug(e.target.value)}
              className="appearance-none bg-neutral-900 border border-neutral-700 rounded-md pl-3 pr-9 py-2 text-sm text-neutral-100 focus:outline-none focus:border-neutral-500 min-w-[300px] cursor-pointer"
            >
              {events.map(e => (
                <option key={e.slug} value={e.slug} className="bg-neutral-900 text-neutral-100">
                  {e.heldCount > 0 ? '✓ ' : ''}{e.cityName} · {metricLabel(e.metric)} · {fmtDate(e.targetDate)}
                  {e.heldCount > 0 ? `  (holding ${e.heldCount})` : ''}
                  {e.actionableCount > 0 ? `  (${e.actionableCount} new)` : ''}
                </option>
              ))}
            </select>
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-neutral-400 text-xs">▾</span>
          </div>
          {selected && (
            <a
              href={`${POLY_EVENT_URL}${selected.slug}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-blue-400 hover:text-blue-300 underline underline-offset-2"
            >
              view on Polymarket ↗
            </a>
          )}
          <span className="text-xs text-neutral-600">{events.length} events scanned</span>
        </div>

        {selected && (
          <div className="rounded-xl border border-neutral-800 bg-neutral-900/40 overflow-hidden">
            {/* forecast header */}
            <div className="px-5 py-4 border-b border-neutral-800 flex flex-wrap items-baseline gap-x-6 gap-y-1">
              <div className="text-lg font-semibold text-neutral-100">
                Forecast {(selected.ensembleMean - selected.bias).toFixed(1)}°{selected.unit}
              </div>
              {Math.abs(selected.bias) >= 0.05 && (
                <div className="text-xs text-neutral-500">
                  raw {selected.ensembleMean.toFixed(1)}°{selected.unit} · bias {selected.bias >= 0 ? '+' : ''}{selected.bias.toFixed(1)}°{selected.unit}
                </div>
              )}
              <div className="text-xs text-neutral-500">± {selected.ensembleStd.toFixed(1)}°{selected.unit} spread</div>
              <div className="text-xs text-neutral-500">{selected.members} ensemble members</div>
            </div>

            {/* bucket table */}
            <table className="w-full text-sm">
              <thead>
                <tr className="text-neutral-500 text-xs uppercase tracking-wider border-b border-neutral-800">
                  <th className="text-left font-medium px-5 py-2.5">Range</th>
                  <th className="text-right font-medium px-3 py-2.5">Forecast</th>
                  <th className="text-right font-medium px-3 py-2.5">Market</th>
                  <th className="text-right font-medium px-3 py-2.5">Edge</th>
                  <th className="text-right font-medium px-3 py-2.5">Net</th>
                  <th className="text-right font-medium px-3 py-2.5">Size</th>
                  <th className="text-left font-medium px-5 py-2.5">Status</th>
                </tr>
              </thead>
              <tbody>
                {selected.signals.map(s => {
                  const yesEdge = s.model_probability - s.market_probability
                  const net = (yesEdge >= 0 ? 1 : -1) * (s.net_edge ?? 0)
                  const heldPos = held[s.market_id]
                  const rowBg = heldPos ? 'bg-amber-500/5' : s.actionable ? 'bg-green-500/5' : ''
                  return (
                    <tr key={s.market_id} className={`border-b border-neutral-800/60 ${rowBg}`}>
                      <td className="px-5 py-2.5 text-neutral-200 font-medium">
                        {s.bucket_label}
                        {heldPos && (
                          <span className="ml-2 text-[10px] font-semibold uppercase text-amber-400 border border-amber-500/40 rounded px-1 py-0.5">held</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-right tabular-nums text-neutral-300">{pct(s.model_probability)}</td>
                      <td className="px-3 py-2.5 text-right tabular-nums text-neutral-300">{pct(s.market_probability)}</td>
                      <td className={`px-3 py-2.5 text-right tabular-nums ${yesEdge >= 0 ? 'text-green-400' : 'text-red-400'}`}>{signedPct(yesEdge)}</td>
                      <td className={`px-3 py-2.5 text-right tabular-nums ${net >= 0 ? 'text-green-400' : 'text-red-400'}`}>{signedPct(net)}</td>
                      <td className="px-3 py-2.5 text-right tabular-nums text-neutral-300">{s.actionable ? `$${(s.suggested_size ?? 0).toFixed(0)}` : '—'}</td>
                      <td className="px-5 py-2.5">
                        {heldPos
                          ? <span className="text-amber-400 font-medium">● holding {heldPos.direction.toUpperCase()} @ {(heldPos.entry_price * 100).toFixed(0)}¢</span>
                          : s.actionable
                            ? <span className="text-green-400">would buy {(s.direction || '').toUpperCase()} @ {((s.entry_price ?? 0) * 100).toFixed(0)}¢</span>
                            : <span className="text-neutral-500">{filterReason(s)}</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

import type { BiasSegment, CalibrationSummary } from '../types'

function signedPct(x: number): string {
  return `${x >= 0 ? '+' : ''}${(x * 100).toFixed(1)}%`
}

function usd(x: number): string {
  return `${x >= 0 ? '+' : '-'}$${Math.abs(x).toFixed(2)}`
}

/** Format the scoreboard-epoch ISO (UTC-naive from the backend) as a readable date. */
function fmtEpoch(iso: string): string {
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z')
  if (isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

/** Shared cohort table: one row per segment, standard scorecard columns. */
function CohortTable({ title, rows, amber, footer }: {
  title: string
  rows: BiasSegment[]
  amber?: (label: string) => boolean
  footer: string
}) {
  if (!rows.length) return null
  return (
    <div className="mt-5 border-t border-neutral-800 pt-4">
      <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-2">{title}</div>
      <table className="w-full text-sm tabular-nums">
        <thead>
          <tr className="text-[11px] uppercase tracking-wider text-neutral-500 text-right">
            <th className="text-left font-medium py-1">Cohort</th>
            <th className="font-medium">Open</th>
            <th className="font-medium">Settled</th>
            <th className="font-medium">Win rate</th>
            <th className="font-medium">Total P&amp;L</th>
            <th className="font-medium">Brier</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className="text-right border-t border-neutral-800/60">
              <td className="text-left py-1.5">
                <span className={amber?.(r.label) ? 'text-amber-400' : 'text-neutral-200'}>
                  {r.label}
                </span>
              </td>
              <td className="text-neutral-300">
                {r.open_trades}
                <span className="text-neutral-600"> (${r.open_exposure.toFixed(0)})</span>
              </td>
              <td className="text-neutral-300">{r.settled}</td>
              <td className={r.settled ? '' : 'text-neutral-600'}>
                {r.settled ? `${(r.win_rate * 100).toFixed(0)}%` : '—'}
              </td>
              <td className={r.settled ? (r.total_pnl >= 0 ? 'text-green-400' : 'text-red-400') : 'text-neutral-600'}>
                {r.settled ? usd(r.total_pnl) : '—'}
              </td>
              <td className={r.settled ? '' : 'text-neutral-600'}>
                {r.brier_score != null ? r.brier_score.toFixed(3) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-xs text-neutral-500 mt-2">{footer}</p>
    </div>
  )
}

/** Corrected-vs-uncorrected cohort table: does the bias model actually help? */
function BiasCohorts({ segments }: { segments: BiasSegment[] }) {
  if (!segments.length) return null
  const order: Record<string, number> = { corrected: 0, uncorrected: 1 }
  const rows = [...segments].sort((a, b) => (order[a.label] ?? 9) - (order[b.label] ?? 9))
  const anySettled = rows.some((r) => r.settled > 0)
  return (
    <CohortTable
      title="Bias model: corrected vs uncorrected cities"
      rows={rows}
      amber={(l) => l === 'uncorrected'}
      footer={anySettled
        ? 'If "uncorrected" trails on win rate / P&L / Brier, the missing bias is leaking. Both should track each other if the market-gap guardrail is covering for it.'
        : 'Tagged and waiting on settlement — win rate, P&L, and Brier per cohort fill in as these trades resolve.'}
    />
  )
}

/** Probation split: the same active record with vs without the watch city (chicago). */
function WatchCohorts({ segments }: { segments: BiasSegment[] }) {
  if (!segments.length) return null
  const rows = [...segments].sort((a, b) =>
    (a.label.startsWith('all') ? 0 : 1) - (b.label.startsWith('all') ? 0 : 1))
  return (
    <CohortTable
      title="Chicago probation: record with vs without it"
      rows={rows}
      amber={(l) => l.startsWith('all')}
      footer={'Chicago failed one out-of-sample half in three straight backtests but stays live for evidence. If "ex-chicago" consistently beats "all cities" on P&L / Brier, chicago is hurting — park it.'}
    />
  )
}

function Metric({ label, value, hint, tone = 'neutral' }: {
  label: string; value: string; hint?: string; tone?: 'pos' | 'neg' | 'neutral'
}) {
  const color = tone === 'pos' ? 'text-green-400' : tone === 'neg' ? 'text-red-400' : 'text-neutral-100'
  return (
    <div className="min-w-[140px]">
      <div className="text-[11px] uppercase tracking-wider text-neutral-500">{label}</div>
      <div className={`text-xl font-semibold tabular-nums mt-1 ${color}`}>{value}</div>
      {hint && <div className="text-xs text-neutral-500 mt-0.5">{hint}</div>}
    </div>
  )
}

export function Scoreboard({ calibration, biasSegments = [], watchSegments = [], epoch = null }: {
  calibration: CalibrationSummary | null
  biasSegments?: BiasSegment[]
  watchSegments?: BiasSegment[]
  epoch?: string | null
}) {
  const settled = calibration?.total_with_outcome ?? 0

  return (
    <section>
      <h2 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2">
        Scoreboard &amp; calibration
      </h2>
      {epoch && (
        <p className="text-[11px] text-neutral-500 mb-2">
          Scored from the blend model going live · {fmtEpoch(epoch)}. Earlier trades are kept
          (still in the trade log) but excluded here so the new model&apos;s record reads clean.
        </p>
      )}
      <div className="rounded-xl border border-neutral-800 bg-neutral-900/40 p-5">
        {settled === 0 ? (
          <div className="text-sm text-neutral-400 leading-relaxed">
            No settled trades yet. Accuracy, Brier score, and predicted-vs-actual edge appear here once
            markets resolve.
            <div className="text-xs text-neutral-500 mt-2">
              This is the honest finish line: does the model beat the market price, <em>net of costs</em>?
            </div>
            <WatchCohorts segments={watchSegments} />
            <BiasCohorts segments={biasSegments} />
          </div>
        ) : (
          <>
            <div className="flex flex-wrap gap-x-8 gap-y-4">
              <Metric label="Settled trades" value={`${settled}`} />
              <Metric
                label="Direction accuracy"
                value={`${(calibration!.accuracy * 100).toFixed(0)}%`}
                hint="how often the predicted side won"
              />
              <Metric
                label="Brier score"
                value={calibration!.brier_score.toFixed(3)}
                hint="0 = perfect · 0.25 = coin-flip · lower is better"
              />
              <Metric label="Avg predicted edge" value={signedPct(calibration!.avg_predicted_edge)} />
              <Metric
                label="Avg actual edge"
                value={signedPct(calibration!.avg_actual_edge)}
                hint="realized vs predicted = calibration"
                tone={calibration!.avg_actual_edge >= 0 ? 'pos' : 'neg'}
              />
            </div>
            <p className="text-xs text-neutral-500 mt-4">
              The honest finish line: the predicted edge should be matched by the actual edge, net of costs.
            </p>
            <WatchCohorts segments={watchSegments} />
            <BiasCohorts segments={biasSegments} />
          </>
        )}
      </div>
    </section>
  )
}

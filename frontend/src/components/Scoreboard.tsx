import type { CalibrationSummary } from '../types'

function signedPct(x: number): string {
  return `${x >= 0 ? '+' : ''}${(x * 100).toFixed(1)}%`
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

export function Scoreboard({ calibration }: { calibration: CalibrationSummary | null }) {
  const settled = calibration?.total_with_outcome ?? 0

  return (
    <section>
      <h2 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2">
        Scoreboard &amp; calibration
      </h2>
      <div className="rounded-xl border border-neutral-800 bg-neutral-900/40 p-5">
        {settled === 0 ? (
          <div className="text-sm text-neutral-400 leading-relaxed">
            No settled trades yet. Accuracy, Brier score, and predicted-vs-actual edge appear here once
            markets resolve.
            <div className="text-xs text-neutral-500 mt-2">
              This is the honest finish line: does the model beat the market price, <em>net of costs</em>?
            </div>
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
          </>
        )}
      </div>
    </section>
  )
}

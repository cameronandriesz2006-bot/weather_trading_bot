import type { WorkingOrder } from '../types'

function statusTone(s: string): string {
  if (s === 'FILLED') return 'text-green-400'
  if (s === 'PARTIALLY_FILLED') return 'text-amber-400'
  if (s === 'OPEN') return 'text-sky-400'
  return 'text-neutral-500' // EXPIRED / CANCELLED / REJECTED
}

function fmtExpiry(sec?: number | null): string {
  if (sec == null) return '—'
  if (sec <= 0) return 'expiring…'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

/** Resting MAKER limit orders: posted-but-not-yet-(fully)-filled. Distinct from filled positions
 *  (which show in Positions & trades). A filled order becomes a position; an unfilled one expires. */
export function WorkingOrders({ orders }: { orders: WorkingOrder[] }) {
  const open = orders.filter((o) => o.status === 'OPEN' || o.status === 'PARTIALLY_FILLED')
  const openCash = open.reduce((a, o) => a + (o.intended_cash || 0), 0)

  return (
    <section>
      <h2 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2">
        Resting orders (maker)
        {open.length > 0 && (
          <span className="ml-2 text-neutral-500 normal-case font-normal tracking-normal">
            {open.length} open · ${openCash.toFixed(0)} posted
          </span>
        )}
      </h2>
      <div className="rounded-xl border border-neutral-800 bg-neutral-900/40 overflow-hidden">
        {orders.length === 0 ? (
          <div className="p-5 text-sm text-neutral-400 leading-relaxed">
            No resting orders right now. When the bot finds an edge it posts a limit order here and
            waits for the day&apos;s flow to fill it — earning the spread instead of paying it. Filled
            orders become positions above; unfilled ones auto-expire.
          </div>
        ) : (
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="text-[11px] uppercase tracking-wider text-neutral-500 text-right border-b border-neutral-800">
                <th className="text-left font-medium px-4 py-2">Market</th>
                <th className="font-medium">Side</th>
                <th className="font-medium">Limit</th>
                <th className="font-medium">Size</th>
                <th className="font-medium">Filled</th>
                <th className="font-medium">Status</th>
                <th className="font-medium px-4">Expires</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.order_id} className="text-right border-b border-neutral-800/40">
                  <td className="text-left px-4 py-2">
                    <div className="text-neutral-200">
                      {o.city_name} · {o.bucket_label}
                    </div>
                    <div className="text-[11px] text-neutral-500">
                      {o.metric} · {o.target_date}
                    </div>
                  </td>
                  <td className={o.direction === 'yes' ? 'text-green-400' : 'text-red-400'}>
                    {o.direction?.toUpperCase()}
                  </td>
                  <td className="text-neutral-300">{(o.limit_price * 100).toFixed(1)}¢</td>
                  <td className="text-neutral-400">${o.intended_cash.toFixed(0)}</td>
                  <td className={o.fill_pct > 0 ? 'text-amber-300' : 'text-neutral-600'}>
                    {(o.fill_pct * 100).toFixed(0)}%
                  </td>
                  <td className={statusTone(o.status)}>
                    {o.status.replace('_', ' ').toLowerCase()}
                  </td>
                  <td className="px-4 text-neutral-400">{fmtExpiry(o.expires_in_seconds)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}

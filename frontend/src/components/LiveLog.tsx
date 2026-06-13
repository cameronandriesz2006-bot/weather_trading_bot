import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchEvents } from '../api'

const TYPE_COLOR: Record<string, string> = {
  success: 'text-green-400',
  error: 'text-red-400',
  warning: 'text-amber-400',
  data: 'text-blue-400',
  trade: 'text-purple-400',
  info: 'text-neutral-400',
}

function fmtTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString('en-US', { hour12: false })
  } catch {
    return '--:--:--'
  }
}

export function LiveLog() {
  const [open, setOpen] = useState(false)

  // Only poll while expanded, to keep things light.
  const { data } = useQuery({
    queryKey: ['events'],
    queryFn: () => fetchEvents(60),
    refetchInterval: 5000,
    enabled: open,
  })

  const logs = (data ?? []).filter(e => e.type !== 'heartbeat')

  return (
    <section>
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2 hover:text-neutral-200"
      >
        <span className="text-neutral-500">{open ? '▾' : '▸'}</span>
        Live event log{open && logs.length > 0 ? ` (${logs.length})` : ''}
      </button>
      {open && (
        <div className="rounded-xl border border-neutral-800 bg-neutral-900/40 max-h-72 overflow-y-auto p-4 font-mono text-xs space-y-1">
          {logs.length === 0 ? (
            <div className="text-neutral-600">Waiting for events…</div>
          ) : (
            logs.slice().reverse().map((e, i) => (
              <div key={i} className="flex gap-3">
                <span className="text-neutral-600 tabular-nums shrink-0">{fmtTime(e.timestamp)}</span>
                <span className={TYPE_COLOR[e.type] ?? 'text-neutral-400'}>{e.message}</span>
              </div>
            ))
          )}
        </div>
      )}
    </section>
  )
}

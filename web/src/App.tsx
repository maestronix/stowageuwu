import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, Bot, Database, HardDrive, RefreshCw } from 'lucide-react'
import { CartesianGrid, ComposedChart, Bar, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

type Summary = {
  attachment_count: number
  total_bytes: number
  growth_24h_bytes: number
  growth_30d_bytes: number
  scanner: { phase: string; started_at: string | null; completed_at: string | null; channels_total: number; channels_done: number; messages_scanned: number; attachments_found: number; last_error: string | null; updated_at: string } | null
}
type Point = { timestamp: string; growth_bytes: number; cumulative_bytes: number; attachment_count: number }
type Growth = { granularity: 'hour' | 'day'; points: Point[] }
type Preset = '24h' | '7d' | '30d' | '1y' | 'custom'

const fmtBytes = (value: number) => {
  if (value < 1000) return `${value.toLocaleString()} B`
  const units = ['kB', 'MB', 'GB', 'TB', 'PB']
  let n = value / 1000, i = 0
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++ }
  return `${n.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${units[i]}`
}
const fmtCount = (n: number) => n.toLocaleString()
const localInput = (date: Date) => {
  const offset = date.getTimezoneOffset() * 60000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}
const rangeFor = (preset: Preset) => {
  const end = new Date(), start = new Date(end)
  if (preset === '24h') start.setHours(start.getHours() - 24)
  if (preset === '7d') start.setDate(start.getDate() - 7)
  if (preset === '30d') start.setDate(start.getDate() - 30)
  if (preset === '1y') start.setFullYear(start.getFullYear() - 1)
  return { start: localInput(start), end: localInput(end) }
}
const shortDate = (iso: string, hourly: boolean) => new Intl.DateTimeFormat(undefined, hourly ? { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' } : { day: '2-digit', month: 'short' }).format(new Date(iso))

export default function App() {
  const [preset, setPreset] = useState<Preset>('30d')
  const [range, setRange] = useState(() => rangeFor('30d'))
  const [summary, setSummary] = useState<Summary | null>(null)
  const [growth, setGrowth] = useState<Growth | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try {
      const [s, g] = await Promise.all([
        fetch('/api/summary').then(r => { if (!r.ok) throw new Error('Summary API request failed'); return r.json() as Promise<Summary> }),
        fetch(`/api/growth?start=${encodeURIComponent(new Date(range.start).toISOString())}&end=${encodeURIComponent(new Date(range.end).toISOString())}`).then(r => { if (!r.ok) throw new Error('Growth API request failed'); return r.json() as Promise<Growth> }),
      ])
      setSummary(s); setGrowth(g)
    } catch (e) { setError(e instanceof Error ? e.message : 'Unknown error') }
    finally { setLoading(false) }
  }, [range])

  useEffect(() => { void load() }, [load])
  useEffect(() => { const timer = window.setInterval(() => { void load() }, 60_000); return () => window.clearInterval(timer) }, [load])

  const chartData = useMemo(() => (growth?.points ?? []).map(p => ({
    ...p,
    label: shortDate(p.timestamp, growth?.granularity === 'hour'),
    growth_gb: p.growth_bytes / 1_000_000_000,
    cumulative_gb: p.cumulative_bytes / 1_000_000_000,
  })), [growth])
  const choosePreset = (value: Preset) => { setPreset(value); if (value !== 'custom') setRange(rangeFor(value)) }
  const progress = summary?.scanner && summary.scanner.channels_total > 0 ? Math.min(100, summary.scanner.channels_done / summary.scanner.channels_total * 100) : 0
  const scannerLabel = summary?.scanner?.phase === 'backfill' ? 'Initial backfill' : summary?.scanner?.phase === 'reconciliation' ? 'Reconciliation' : 'Idle'

  return <main className="shell">
    <header className="topbar">
      <div className="brand"><div className="brand-mark"><Database size={21}/></div><div><div className="brand-name">stowageuwu</div><div className="brand-sub">DISCORD STORAGE ANALYTICS</div></div></div>
      <button className="refresh" onClick={() => void load()} disabled={loading}><RefreshCw size={15} className={loading ? 'spin' : ''}/> Refresh</button>
    </header>

    <section className="page-heading"><div><div className="eyebrow">OVERVIEW</div><h1>Storage analytics</h1><p>Historical attachment inventory and upload growth.</p></div><div className="live-pill"><span/> Live data</div></section>

    {error && <div className="error-banner">{error}. Check that the API and database are running.</div>}

    <section className="metrics">
      <article className="metric-card"><div className="metric-label"><HardDrive size={15}/> Lifetime storage</div><div className="metric-value">{summary ? fmtBytes(summary.total_bytes) : '—'}</div><div className="metric-foot">All observed attachments</div></article>
      <article className="metric-card"><div className="metric-label"><Database size={15}/> Attachments</div><div className="metric-value">{summary ? fmtCount(summary.attachment_count) : '—'}</div><div className="metric-foot">Unique Discord attachment IDs</div></article>
      <article className="metric-card"><div className="metric-label"><Activity size={15}/> Growth · 24 hours</div><div className="metric-value">{summary ? fmtBytes(summary.growth_24h_bytes) : '—'}</div><div className="metric-foot">By original message timestamp</div></article>
      <article className="metric-card"><div className="metric-label"><Activity size={15}/> Growth · 30 days</div><div className="metric-value">{summary ? fmtBytes(summary.growth_30d_bytes) : '—'}</div><div className="metric-foot">By original message timestamp</div></article>
    </section>

    <section className="panel chart-panel">
      <div className="panel-head"><div><div className="eyebrow">HISTORICAL TREND</div><h2>Storage growth</h2><p>Cumulative storage and newly uploaded bytes in one timeline.</p></div>
        <div className="presets">{(['24h','7d','30d','1y','custom'] as Preset[]).map(p => <button key={p} className={preset === p ? 'selected' : ''} onClick={() => choosePreset(p)}>{p === 'custom' ? 'Custom' : p}</button>)}</div>
      </div>
      {preset === 'custom' && <div className="custom-range"><label>From<input type="datetime-local" value={range.start} onChange={e => setRange(v => ({ ...v, start: e.target.value }))}/></label><label>To<input type="datetime-local" value={range.end} onChange={e => setRange(v => ({ ...v, end: e.target.value }))}/></label><button className="apply" onClick={() => void load()}>Apply range</button></div>}
      <div className="legend"><span><i className="legend-bar"/> New storage</span><span><i className="legend-line"/> Lifetime storage</span><span className="granularity">{growth?.granularity === 'hour' ? 'Hourly buckets' : 'Daily buckets'}</span></div>
      <div className="chart-wrap">{loading && !growth ? <div className="chart-empty">Loading analytics…</div> : chartData.length ? <ResponsiveContainer width="100%" height="100%"><ComposedChart data={chartData} margin={{ top: 10, right: 12, left: 8, bottom: 4 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#202b3c" vertical={false}/>
        <XAxis dataKey="label" tick={{ fill: '#8190a6', fontSize: 11 }} axisLine={{ stroke: '#263246' }} tickLine={false} minTickGap={28}/>
        <YAxis yAxisId="growth" orientation="left" tick={{ fill: '#8190a6', fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={v => `${v} GB`} width={64}/>
        <YAxis yAxisId="total" orientation="right" tick={{ fill: '#8190a6', fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={v => `${v} GB`} width={72}/>
        <Tooltip contentStyle={{ background: '#101827', border: '1px solid #29364a', borderRadius: 10, color: '#e8eef7' }} labelStyle={{ color: '#a8b5c8' }} formatter={(value: unknown, name: unknown) => [fmtBytes(Number(value) * 1_000_000_000), name === 'growth_gb' ? 'New storage' : 'Lifetime storage']}/>
        <Bar yAxisId="growth" dataKey="growth_gb" name="growth_gb" fill="#647ff2" radius={[3,3,0,0]} maxBarSize={24}/>
        <Line yAxisId="total" dataKey="cumulative_gb" name="cumulative_gb" type="monotone" stroke="#39c6a5" strokeWidth={2.5} dot={false} activeDot={{ r: 4, fill: '#39c6a5' }}/>
      </ComposedChart></ResponsiveContainer> : <div className="chart-empty">No attachment data in this time range.</div>}</div>
    </section>

    <section className="panel scanner-panel"><div className="scanner-icon"><Bot size={19}/></div><div className="scanner-main"><div className="scanner-title">Scanner status <span className={`status-tag ${summary?.scanner?.phase === 'idle' ? 'idle' : 'working'}`}>{scannerLabel}</span></div><div className="scanner-meta">{summary?.scanner ? `${fmtCount(summary.scanner.messages_scanned)} messages scanned in current run · ${fmtCount(summary.scanner.channels_done)} / ${fmtCount(summary.scanner.channels_total)} channels` : 'Waiting for scanner status'}</div>{summary?.scanner && summary.scanner.channels_total > 0 && <div className="progress-track"><div style={{ width: `${progress}%` }}/></div>}{summary?.scanner?.last_error && <div className="scanner-error">Last error: {summary.scanner.last_error}</div>}</div><div className="scanner-updated">{summary?.scanner?.updated_at ? `Updated ${new Date(summary.scanner.updated_at).toLocaleTimeString()}` : ''}</div></section>
    <footer>All sizes are exact attachment metadata from Discord. Files are never downloaded. Timestamps are based on the original message creation time.</footer>
  </main>
}

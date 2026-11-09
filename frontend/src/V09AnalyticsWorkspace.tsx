import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import {
  Activity,
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Database,
  Info,
  Search,
  ShieldCheck,
  Ship,
} from 'lucide-react'

const API = 'http://127.0.0.1:8000'
const PAGE_SIZE = 12

type V09Status = {
  api_module_version: string
  mode: string
  historical_anomaly_index: { ready: boolean; rows: number; start_date?: string; end_date?: string; size_mb?: number; error?: string }
  phase3_candidates: { ready: boolean; rows: number; sar_records_with_candidates?: number; size_mb?: number; error?: string }
  recent_grid: { ready: boolean; identity_available?: boolean; record_granularity?: string; by_kind?: Array<{ kind: string; n: number; first_day: string; last_day: string; regions: number }>; size_mb?: number; error?: string }
  dependencies?: { additional_ml_packages_required?: boolean }
  notes?: string[]
}

type Overview = {
  data_mode: string
  actual_start_date: string | null
  actual_end_date: string | null
  total_observations: number
  unique_vessels: number
  flagged_observations: number
  baseline_observations: number
  mean_saved_anomaly_score: number | null
  min_saved_anomaly_score: number | null
  max_saved_anomaly_score: number | null
  by_status: Array<{ label: string; n: number }>
  by_vessel_type: Array<{ label: string; n: number }>
  by_region: Array<{ label: string; n: number }>
  interpretation: string
}

type QueueRow = {
  id: number
  date_day: string
  date: string
  mmsi: string
  shipName?: string
  vesselType?: string
  lat?: number | null
  lon?: number | null
  speed_knots?: string | number | null
  speed_change_knots?: string | number | null
  time_gap_hours?: string | number | null
  anomaly_score?: string | number | null
  anomaly_flag: string
  anomaly_status?: string
  region_iso3?: string
  region_label?: string
}

type QueueResponse = {
  total_matching: number
  observations: QueueRow[]
  note: string
}

type FeatureDeviation = {
  feature: string
  label: string
  observed_value: number | null
  cohort_mean: number | null
  cohort_std: number | null
  cohort_n: number
  z_score: number | null
  deviation_level: string
}

type Explanation = {
  observation: QueueRow
  saved_model_signal: { anomaly_flag: string; anomaly_status?: string; anomaly_score: number | null; score_origin: string }
  feature_deviations: FeatureDeviation[]
  comparison_cohort: { basis: string; vessel_type?: string | null; minimum_feature_sample_count: number }
  methodology_note: string
}

type AssociationOverview = {
  candidate_rows: number
  sar_records_with_candidates: number
  unique_candidate_vessels: number
  mean_distance_km: number | null
  mean_time_gap_min: number | null
  mean_ranking_score: number | null
  candidate_rank_distribution: Array<{ candidate_rank: number; n: number }>
  interpretation: string
}

function fmt(value: unknown, digits = 2): string {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : '—'
}

function count(value: unknown): string {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toLocaleString('en-IN') : '—'
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`, { cache: 'no-store' })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = body?.detail?.message || body?.detail || `HTTP ${response.status}`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return response.json() as Promise<T>
}

function MetricCard({ label, value, note, icon }: { label: string; value: string; note: string; icon: ReactNode }) {
  return (
    <div className="v09-metric">
      <div className="v09-metric-top"><span>{label}</span><span className="v09-icon">{icon}</span></div>
      <strong>{value}</strong>
      <small>{note}</small>
    </div>
  )
}

function deviationText(level: string): string {
  switch (level) {
    case 'very_high_deviation': return 'Very high deviation'
    case 'elevated_deviation': return 'Elevated deviation'
    case 'moderate_deviation': return 'Moderate deviation'
    case 'near_cohort_baseline': return 'Near cohort baseline'
    default: return 'Not available'
  }
}

export default function V09AnalyticsWorkspace({ startDate, endDate, rangeReady, refreshKey }: {
  startDate: string
  endDate: string
  rangeReady: boolean
  refreshKey: number
}) {
  const [status, setStatus] = useState<V09Status | null>(null)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [queue, setQueue] = useState<QueueResponse | null>(null)
  const [association, setAssociation] = useState<AssociationOverview | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [explanation, setExplanation] = useState<Explanation | null>(null)
  const [searchDraft, setSearchDraft] = useState('')
  const [search, setSearch] = useState('')
  const [region, setRegion] = useState('ALL')
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [explanationLoading, setExplanationLoading] = useState(false)
  const [error, setError] = useState('')
  const [explanationError, setExplanationError] = useState('')
  const queueRequest = useRef(0)
  const explanationRequest = useRef(0)

  const loadStaticPanels = useCallback(async () => {
    try {
      const [statusData, associationData] = await Promise.all([
        getJson<V09Status>('/api/v09/status'),
        getJson<AssociationOverview>('/api/v09/association-overview'),
      ])
      setStatus(statusData)
      setAssociation(associationData)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load v0.9 data-source status.')
    }
  }, [])

  const loadOverview = useCallback(async () => {
    if (!rangeReady || !startDate || !endDate) return
    try {
      const params = new URLSearchParams({ start_date: startDate, end_date: endDate })
      const data = await getJson<Overview>(`/api/v09/overview?${params}`)
      setOverview(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load the historical model overview.')
      setOverview(null)
    }
  }, [rangeReady, startDate, endDate])

  const loadQueue = useCallback(async () => {
    if (!rangeReady || !startDate || !endDate) return
    const requestId = ++queueRequest.current
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams({
        start_date: startDate,
        end_date: endDate,
        limit: String(PAGE_SIZE),
        offset: String(offset),
      })
      if (search) params.set('search', search)
      if (region !== 'ALL') params.set('region_iso3', region)
      const data = await getJson<QueueResponse>(`/api/v09/review-queue?${params}`)
      if (requestId !== queueRequest.current) return
      setQueue(data)
      setSelectedId((current) => data.observations.some((row) => row.id === current)
        ? current
        : (data.observations[0]?.id ?? null))
    } catch (err) {
      if (requestId !== queueRequest.current) return
      setError(err instanceof Error ? err.message : 'Could not load the review queue.')
      setQueue(null)
      setSelectedId(null)
    } finally {
      if (requestId === queueRequest.current) setLoading(false)
    }
  }, [rangeReady, startDate, endDate, offset, search, region])

  useEffect(() => {
    void loadStaticPanels()
  }, [loadStaticPanels, refreshKey])

  useEffect(() => {
    void loadOverview()
    void loadQueue()
  }, [loadOverview, loadQueue, refreshKey])

  useEffect(() => {
    if (selectedId === null) {
      setExplanation(null)
      setExplanationError('')
      return
    }
    const requestId = ++explanationRequest.current
    setExplanationLoading(true)
    setExplanationError('')
    getJson<Explanation>(`/api/v09/explain/${selectedId}`)
      .then((data) => {
        if (requestId === explanationRequest.current) setExplanation(data)
      })
      .catch((err) => {
        if (requestId !== explanationRequest.current) return
        setExplanation(null)
        setExplanationError(err instanceof Error ? err.message : 'Could not load observation context.')
      })
      .finally(() => {
        if (requestId === explanationRequest.current) setExplanationLoading(false)
      })
  }, [selectedId, refreshKey])

  const regionOptions = useMemo(() => {
    const fromOverview = (overview?.by_region || []).map((item) => item.label).filter(Boolean)
    return Array.from(new Set(fromOverview)).sort()
  }, [overview])

  const currentPage = Math.floor(offset / PAGE_SIZE) + 1
  const pageCount = Math.max(1, Math.ceil((queue?.total_matching || 0) / PAGE_SIZE))

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setOffset(0)
    setSearch(searchDraft.trim())
  }

  function resetQueueFilters() {
    setSearchDraft('')
    setSearch('')
    setRegion('ALL')
    setOffset(0)
  }

  return (
    <section className="v09-workspace" aria-label="DARK-FLEET v0.9 Model Intelligence">
      <style>{`
        .v09-workspace { margin: 22px 0 26px; color: #e7f0f8; scroll-margin-top: 16px; }
        .v09-workspace * { box-sizing: border-box; }
        .v09-header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; padding:19px 20px; border:1px solid #244356; border-radius:13px; background:linear-gradient(115deg,#102a3b,#0b1b2a 70%); }
        .v09-kicker { color:#72e4f0; font-size:9px; font-weight:850; letter-spacing:1.6px; }
        .v09-header h2 { margin:7px 0 5px; font-size:20px; letter-spacing:-.5px; }
        .v09-header p { margin:0; color:#91a8bb; font-size:11px; line-height:1.6; max-width:800px; }
        .v09-readonly { flex-shrink:0; padding:7px 9px; border:1px solid #286c64; border-radius:7px; color:#9aecd0; background:#10352f; font-size:9px; font-weight:800; letter-spacing:.7px; }
        .v09-source-strip { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin:12px 0; }
        .v09-source { min-width:0; padding:12px 13px; border:1px solid #1e3447; border-radius:10px; background:#0b1927; }
        .v09-source-top { display:flex; justify-content:space-between; align-items:center; gap:8px; color:#8ea5b9; font-size:9px; text-transform:uppercase; letter-spacing:.7px; }
        .v09-source strong { display:block; margin-top:8px; font-size:19px; color:#e8f4fb; }
        .v09-source small { display:block; margin-top:4px; color:#8299af; font-size:9px; line-height:1.5; }
        .v09-ok { color:#8ce2ba; }
        .v09-panel { border:1px solid #1d3347; border-radius:12px; background:#0c1a29; min-width:0; }
        .v09-panel-title { padding:14px 16px; border-bottom:1px solid #1c3044; }
        .v09-panel-title h3 { margin:0; font-size:13px; }
        .v09-panel-title p { margin:5px 0 0; color:#849bb0; font-size:10px; line-height:1.5; }
        .v09-summary-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:12px 0; }
        .v09-metric { padding:13px; border:1px solid #1d3448; border-radius:10px; background:#0b1927; min-width:0; }
        .v09-metric-top { display:flex; justify-content:space-between; gap:8px; color:#8ea5b9; font-size:9px; line-height:1.4; }
        .v09-icon { color:#6ce3f2; }
        .v09-metric strong { display:block; margin-top:11px; color:#eaf5fc; font-size:23px; letter-spacing:-.7px; overflow-wrap:anywhere; }
        .v09-metric small { display:block; margin-top:4px; color:#8198ad; font-size:9px; }
        .v09-main-grid { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(320px,1fr); gap:12px; align-items:start; margin-top:12px; }
        .v09-panel-body { padding:14px 16px; }
        .v09-toolbar { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
        .v09-toolbar input,.v09-toolbar select { min-height:35px; min-width:0; border:1px solid #294258; border-radius:7px; background:#081522; color:#e5eef7; padding:7px 9px; font-size:10px; }
        .v09-toolbar input { flex:1; }
        .v09-btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; border:1px solid #29465c; border-radius:7px; padding:8px 10px; background:#102538; color:#dceaf5; font-size:10px; font-weight:700; }
        .v09-btn:hover { background:#17354b; }
        .v09-btn:disabled { opacity:.4; cursor:not-allowed; }
        .v09-table-wrap { overflow-x:auto; }
        .v09-table { width:100%; min-width:570px; border-collapse:collapse; }
        .v09-table th { padding:9px 8px; text-align:left; color:#71899f; border-bottom:1px solid #263b4f; font-size:8px; letter-spacing:.8px; }
        .v09-table td { padding:10px 8px; border-bottom:1px solid #1a2d40; color:#b9c9d8; font-size:10px; white-space:nowrap; }
        .v09-table tbody tr { cursor:pointer; }
        .v09-table tbody tr:hover,.v09-table tbody tr.selected { background:#112a3c; }
        .v09-primary { color:#e9f4fb !important; font-weight:750; }
        .v09-score { color:#f5d58f !important; font-variant-numeric:tabular-nums; font-weight:800; }
        .v09-tag { display:inline-flex; border-radius:5px; padding:4px 6px; background:#3a2b19; border:1px solid #644d29; color:#f4d796; font-size:8px; font-weight:800; }
        .v09-empty { padding:25px 10px; color:#8299ae; text-align:center; font-size:11px; }
        .v09-pagination { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:12px; color:#849bb0; font-size:9px; }
        .v09-pagination-actions { display:flex; gap:6px; }
        .v09-detail-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
        .v09-detail-head strong { display:block; font-size:13px; }
        .v09-detail-head small { display:block; margin-top:4px; color:#8299af; font-size:9px; }
        .v09-detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:7px; margin-top:12px; }
        .v09-detail-cell { padding:9px; border:1px solid #1b3043; border-radius:7px; background:#091624; min-width:0; }
        .v09-detail-cell span { display:block; color:#8198ad; font-size:8px; text-transform:uppercase; letter-spacing:.5px; }
        .v09-detail-cell strong { display:block; margin-top:6px; color:#e3eef7; font-size:12px; overflow-wrap:anywhere; }
        .v09-feature { padding:11px 0; border-bottom:1px solid #1d3042; }
        .v09-feature:last-child { border-bottom:0; }
        .v09-feature-top { display:flex; justify-content:space-between; gap:8px; align-items:center; }
        .v09-feature-top strong { font-size:10px; }
        .v09-feature-top span { color:#9ddfea; font-size:9px; }
        .v09-feature-values { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px; margin-top:8px; }
        .v09-feature-values div { color:#8198ad; font-size:8px; }
        .v09-feature-values b { display:block; color:#dce8f2; font-size:10px; margin-top:4px; }
        .v09-note { display:flex; gap:8px; align-items:flex-start; margin-top:12px; padding:10px; border:1px solid #32465a; border-radius:8px; background:#0a1927; color:#96aabd; font-size:9px; line-height:1.55; }
        .v09-error { margin:11px 0; padding:10px 12px; border:1px solid #6c383b; border-radius:8px; background:#2b191e; color:#ffc2c2; font-size:10px; line-height:1.5; }
        .v09-association { margin-top:12px; }
        .v09-association-stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; padding:13px 16px; }
        .v09-association-stats div { min-width:0; }
        .v09-association-stats span { display:block; color:#8198ad; font-size:8px; text-transform:uppercase; letter-spacing:.5px; }
        .v09-association-stats strong { display:block; margin-top:6px; font-size:15px; color:#e7f2fa; overflow-wrap:anywhere; }
        .v09-foot { padding:0 16px 14px; color:#8097ac; font-size:9px; line-height:1.55; }
        @media(max-width:1050px) { .v09-main-grid { grid-template-columns:1fr; } }
        @media(max-width:760px) { .v09-source-strip { grid-template-columns:1fr; } .v09-summary-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .v09-association-stats { grid-template-columns:repeat(2,minmax(0,1fr)); } .v09-header { flex-direction:column; } }
      `}</style>

      <div className="v09-header">
        <div>
          <div className="v09-kicker">DARK-FLEET · RESEARCH INTELLIGENCE · v0.9.0</div>
          <h2>Model intelligence</h2>
          <p>Inspect the existing historical anomaly outputs, review flagged AIS observations and view statistical context for selected features. The current Isolation Forest results are reused, not retrained.</p>
        </div>
        <div className="v09-readonly">READ-ONLY ANALYTICS</div>
      </div>

      {error && <div className="v09-error" role="alert">{error}</div>}

      <div className="v09-source-strip">
        <div className="v09-source">
          <div className="v09-source-top"><span>Historical anomaly index</span><span className={status?.historical_anomaly_index.ready ? 'v09-ok' : ''}>{status?.historical_anomaly_index.ready ? 'READY' : 'CHECKING'}</span></div>
          <strong>{count(status?.historical_anomaly_index.rows)}</strong>
          <small>{status?.historical_anomaly_index.start_date || '—'} to {status?.historical_anomaly_index.end_date || '—'} · Saved vessel-level model results</small>
        </div>
        <div className="v09-source">
          <div className="v09-source-top"><span>Phase 3 candidate index</span><span className={status?.phase3_candidates.ready ? 'v09-ok' : ''}>{status?.phase3_candidates.ready ? 'READY' : 'CHECKING'}</span></div>
          <strong>{count(status?.phase3_candidates.rows)}</strong>
          <small>{count(status?.phase3_candidates.sar_records_with_candidates)} SAR records with ranked proximity candidates</small>
        </div>
        <div className="v09-source">
          <div className="v09-source-top"><span>Recent grid database</span><span className={status?.recent_grid.ready ? 'v09-ok' : ''}>{status?.recent_grid.ready ? 'READY' : 'CHECKING'}</span></div>
          <strong>{count((status?.recent_grid.by_kind || []).reduce((sum, item) => sum + Number(item.n || 0), 0))}</strong>
          <small>AIS/SAR grid-cell-hour aggregates · Identity unavailable · {status?.recent_grid.by_kind?.[0]?.first_day || '—'} to {status?.recent_grid.by_kind?.[0]?.last_day || '—'}</small>
        </div>
      </div>

      <div className="v09-panel">
        <div className="v09-panel-title">
          <h3>Historical model overview</h3>
          <p>Selected dashboard date range: {startDate || '—'} through {endDate || '—'}. The values below include only historical observations available inside that range.</p>
        </div>
        {!rangeReady ? <div className="v09-empty">Waiting for the unified date range…</div> : overview ? (
          <>
            <div className="v09-summary-grid">
              <MetricCard icon={<Database size={14} />} label="OBSERVATIONS IN RANGE" value={count(overview.total_observations)} note={`${overview.actual_start_date || 'No rows'} – ${overview.actual_end_date || 'No rows'}`} />
              <MetricCard icon={<AlertTriangle size={14} />} label="FLAGGED FOR REVIEW" value={count(overview.flagged_observations)} note="Existing saved anomaly flags" />
              <MetricCard icon={<Ship size={14} />} label="UNIQUE MMSI" value={count(overview.unique_vessels)} note="Historical vessel identities" />
              <MetricCard icon={<Activity size={14} />} label="BASELINE OBSERVATIONS" value={count(overview.baseline_observations)} note={`Mean saved score: ${fmt(overview.mean_saved_anomaly_score, 4)}`} />
            </div>
            <div className="v09-panel-body" style={{ paddingTop: 0 }}>
              <div className="v09-detail-grid">
                <div className="v09-detail-cell"><span>Top vessel type</span><strong>{overview.by_vessel_type?.[0]?.label || '—'} · {count(overview.by_vessel_type?.[0]?.n)}</strong></div>
                <div className="v09-detail-cell"><span>Top region</span><strong>{overview.by_region?.[0]?.label || '—'} · {count(overview.by_region?.[0]?.n)}</strong></div>
              </div>
            </div>
          </>
        ) : <div className="v09-empty">Historical overview is unavailable for this range.</div>}
      </div>

      <div className="v09-main-grid">
        <section className="v09-panel">
          <div className="v09-panel-title">
            <h3>Flagged observation review queue</h3>
            <p>{count(queue?.total_matching)} saved flagged observations in the selected date range · ordered by the existing anomaly score</p>
          </div>
          <div className="v09-panel-body">
            <form className="v09-toolbar" onSubmit={submitSearch}>
              <input aria-label="Search review queue" placeholder="Search MMSI, vessel name or type…" value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} />
              <select aria-label="Filter by region" value={region} onChange={(event) => { setRegion(event.target.value); setOffset(0) }}>
                <option value="ALL">All regions</option>
                {regionOptions.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
              <button className="v09-btn" type="submit"><Search size={12} /> Search</button>
              <button className="v09-btn" type="button" onClick={resetQueueFilters}>Reset</button>
            </form>
            <div className="v09-table-wrap">
              <table className="v09-table">
                <thead><tr><th>VESSEL / MMSI</th><th>DATE</th><th>REGION</th><th>SAVED SCORE</th><th>REVIEW</th></tr></thead>
                <tbody>
                  {(queue?.observations || []).map((row) => (
                    <tr key={row.id} className={selectedId === row.id ? 'selected' : ''} onClick={() => setSelectedId(row.id)} aria-selected={selectedId === row.id}>
                      <td><span className="v09-primary">{row.shipName || 'Unknown vessel'}</span><br /><span style={{ color: '#7f96ab', fontSize: 9 }}>{row.mmsi || 'No MMSI'}</span></td>
                      <td>{row.date_day || '—'}</td>
                      <td>{row.region_iso3 || '—'}</td>
                      <td className="v09-score">{fmt(row.anomaly_score, 4)}</td>
                      <td><span className="v09-tag">FLAGGED</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!loading && !queue?.observations?.length && <div className="v09-empty">No flagged observations were found for this date range and filter.</div>}
              {loading && <div className="v09-empty">Loading review queue…</div>}
            </div>
            <div className="v09-pagination">
              <span>Page {count(currentPage)} of {count(pageCount)}</span>
              <div className="v09-pagination-actions">
                <button className="v09-btn" type="button" disabled={offset <= 0 || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft size={12} /> Previous</button>
                <button className="v09-btn" type="button" disabled={offset + PAGE_SIZE >= (queue?.total_matching || 0) || loading} onClick={() => setOffset(offset + PAGE_SIZE)}>Next <ChevronRight size={12} /></button>
              </div>
            </div>
            {queue?.note && <div className="v09-note"><Info size={13} />{queue.note}</div>}
          </div>
        </section>

        <section className="v09-panel">
          <div className="v09-panel-title">
            <h3>Observation context</h3>
            <p>Statistical deviation context for the selected flagged observation</p>
          </div>
          <div className="v09-panel-body">
            {explanationLoading ? <div className="v09-empty">Calculating feature context…</div> : explanationError ? <div className="v09-error">{explanationError}</div> : explanation ? (
              <>
                <div className="v09-detail-head">
                  <div><strong>{explanation.observation.shipName || 'Unknown vessel'}</strong><small>MMSI {explanation.observation.mmsi || '—'} · {explanation.observation.date_day || '—'}</small></div>
                  <span className="v09-tag">SAVED FLAG</span>
                </div>
                <div className="v09-detail-grid">
                  <div className="v09-detail-cell"><span>Saved anomaly score</span><strong>{fmt(explanation.saved_model_signal.anomaly_score, 5)}</strong></div>
                  <div className="v09-detail-cell"><span>Vessel type</span><strong>{explanation.observation.vesselType || 'Unknown'}</strong></div>
                  <div className="v09-detail-cell"><span>Speed</span><strong>{fmt(explanation.observation.speed_knots)} kn</strong></div>
                  <div className="v09-detail-cell"><span>Time gap</span><strong>{fmt(explanation.observation.time_gap_hours)} hr</strong></div>
                </div>
                <div style={{ marginTop: 14, color: '#9bb2c6', fontSize: 9, fontWeight: 800, letterSpacing: '.8px' }}>FEATURE DEVIATION CONTEXT</div>
                {(explanation.feature_deviations || []).map((feature) => (
                  <div className="v09-feature" key={feature.feature}>
                    <div className="v09-feature-top"><strong>{feature.label}</strong><span>{deviationText(feature.deviation_level)}</span></div>
                    <div className="v09-feature-values">
                      <div>Observed<b>{fmt(feature.observed_value, 3)}</b></div>
                      <div>Cohort mean<b>{fmt(feature.cohort_mean, 3)}</b></div>
                      <div>Z-score<b>{fmt(feature.z_score, 2)}</b></div>
                    </div>
                  </div>
                ))}
                <div className="v09-note"><Info size={13} />Comparison cohort: {explanation.comparison_cohort.basis === 'vessel_type' ? `vessel type ${explanation.comparison_cohort.vessel_type}` : 'all vessels'} · at least {count(explanation.comparison_cohort.minimum_feature_sample_count)} usable observations per feature.</div>
                <div className="v09-note">{explanation.methodology_note}</div>
              </>
            ) : <div className="v09-empty">Select a flagged observation to inspect its feature context.</div>}
          </div>
        </section>
      </div>

      <section className="v09-panel v09-association">
        <div className="v09-panel-title">
          <h3>Phase 3 association intelligence</h3>
          <p>Summary of the existing SAR/AIS proximity candidate index. Candidate rankings are not confirmed matches.</p>
        </div>
        {association ? (
          <>
            <div className="v09-association-stats">
              <div><span>Candidate rows</span><strong>{count(association.candidate_rows)}</strong></div>
              <div><span>SAR records with candidates</span><strong>{count(association.sar_records_with_candidates)}</strong></div>
              <div><span>Unique candidate vessels</span><strong>{count(association.unique_candidate_vessels)}</strong></div>
              <div><span>Mean distance</span><strong>{fmt(association.mean_distance_km)} km</strong></div>
            </div>
            <div className="v09-foot">{association.interpretation} Mean time gap: {fmt(association.mean_time_gap_min)} minutes. Mean ranking score: {fmt(association.mean_ranking_score, 4)}.</div>
          </>
        ) : <div className="v09-empty">Loading Phase 3 candidate statistics…</div>}
      </section>

      <div className="v09-note"><ShieldCheck size={13} />Read-only research workspace. Saved anomaly scores are not probabilities or legal conclusions. Feature deviations are descriptive context, not SHAP values and do not reconstruct the Isolation Forest decision path. Recent grid aggregates are not individual vessel tracks.</div>
    </section>
  )
}

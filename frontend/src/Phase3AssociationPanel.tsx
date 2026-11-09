import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { AlertTriangle, ChevronLeft, ChevronRight, Database, RefreshCw, Search, Ship, Target, MapPin } from 'lucide-react'
import './Phase3AssociationPanel.css'

const API = 'http://127.0.0.1:8000'
const PAGE_SIZE = 25

type SarRecord = {
  sar_record_id: string
  sar_region_iso3: string
  sar_region_label: string
  sar_date: string
  sar_entry_timestamp: string
  sar_exit_timestamp: string
  sar_lat: string
  sar_lon: string
  sar_source_detections: string
  candidate_count_in_search_envelope: string
  association_status: string
  nearest_mmsi: string
  nearest_ship_name: string
  nearest_region_iso3: string
  nearest_observation_time: string
  nearest_distance_km: string
  nearest_time_gap_min: string
  nearest_ranking_score: string
  interpretation: string
}

type Candidate = {
  candidate_rank: string
  candidate_mmsi: string
  candidate_ship_name: string
  candidate_vessel_type: string
  candidate_flag: string
  candidate_region_iso3: string
  candidate_observation_time: string
  distance_km: string
  time_gap_min: string
  ranking_score: string
  scenario_ranking_score: number
  distance_score_contribution?: number
  time_score_contribution?: number
  distance_limit_utilization_pct?: number
  time_limit_utilization_pct?: number
  score_formula?: string
  candidate_interpretation: string
  temporal_relation?: 'WITHIN_ACQUISITION_WINDOW' | 'BEFORE_ACQUISITION_WINDOW' | 'AFTER_ACQUISITION_WINDOW' | 'UNKNOWN'
  acquisition_window_duration_min?: number | null
  outside_window_gap_min?: number | null
  temporal_interpretation?: string
}

type RankingDiagnostics = {
  method: string
  distance_weight: number
  time_weight: number
  score_range: number[]
  top_candidate: null | {
    candidate_mmsi: string
    distance_km: number
    time_gap_min: number
    distance_score_contribution: number
    time_score_contribution: number
    scenario_ranking_score: number
    temporal_relation?: string
    acquisition_window_duration_min?: number | null
    outside_window_gap_min?: number | null
    temporal_interpretation?: string
  }
  runner_up_score: number | null
  top_two_score_gap: number | null
  temporal_basis?: string
  interpretation: string
}

type Overview = {
  total_sar_presence_records: number
  records_with_candidates: number
  records_without_candidates: number
  candidate_record_rate_pct: number
  candidate_pairs_broad: number
  region_count: number
  region_counts: Record<string, number>
  sensitivity: Array<Record<string, string>>
  note: string
}

type Phase3Status = { ready: boolean; missing_files: string[]; files: Record<string, { exists: boolean; size_mb: number }>; candidate_index_ready: boolean; sar_presence_records?: number; candidate_pairs_broad?: number; data_window?: { start: string; end_exclusive: string }; note?: string }

type Scenario = 'STRICT' | 'STANDARD' | 'BROAD'
type StatusFilter = 'ALL' | 'HAS_CANDIDATES' | 'NO_CANDIDATES'

function fmt(value: unknown, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '—'
}


function acquisitionDurationLabel(startValue: string, endValue: string) {
  const start = Date.parse(startValue || '')
  const end = Date.parse(endValue || '')
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 'duration unavailable'
  const minutes = Math.max(0, Math.round(Math.abs(end - start) / 60000))
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)
  const mins = minutes % 60
  return [days ? `${days}d` : '', hours ? `${hours}h` : '', mins ? `${mins}m` : '']
    .filter(Boolean).join(' ') || 'under 1m'
}

function parseUtcMillis(value: string | null | undefined): number | null {
  const raw = String(value || '').trim()
  if (!raw) return null
  // GFW AIS presence timestamps can be timezone-naive UTC strings such as
  // "2022-01-03 15:00". Treat those as UTC explicitly instead of browser-local time.
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)
  const isoLike = raw.includes('T') ? raw : raw.replace(/\s+/, 'T')
  const parsed = Date.parse(hasTimezone ? isoLike : `${isoLike}Z`)
  return Number.isFinite(parsed) ? parsed : null
}

function temporalLabel(candidate: Candidate, selected: SarRecord) {
  // Derive the relation from the exact timestamps shown in the UI. This is a
  // defensive fallback when an older/stale API index omits temporal_relation.
  let start = parseUtcMillis(selected.sar_entry_timestamp)
  let end = parseUtcMillis(selected.sar_exit_timestamp)
  const ais = parseUtcMillis(candidate.candidate_observation_time)

  if (start != null && end == null) end = start
  if (end != null && start == null) start = end
  if (start != null && end != null && ais != null) {
    if (end < start) [start, end] = [end, start]
    if (ais < start) return `${fmt((start - ais) / 60000, 1)} min before window`
    if (ais > end) return `${fmt((ais - end) / 60000, 1)} min after window`
    return 'Inside SAR acquisition window'
  }

  if (candidate.temporal_relation === 'WITHIN_ACQUISITION_WINDOW') return 'Inside SAR acquisition window'
  if (candidate.temporal_relation === 'BEFORE_ACQUISITION_WINDOW') return `${fmt(candidate.outside_window_gap_min, 1)} min before window`
  if (candidate.temporal_relation === 'AFTER_ACQUISITION_WINDOW') return `${fmt(candidate.outside_window_gap_min, 1)} min after window`
  return 'Temporal relation unavailable'
}

function temporalWindowNote(selected: SarRecord) {
  const start = parseUtcMillis(selected.sar_entry_timestamp)
  const end = parseUtcMillis(selected.sar_exit_timestamp)
  if (start == null || end == null) {
    return 'A 0-minute interval gap means an AIS timestamp falls somewhere inside the SAR acquisition window. It does not mean the AIS report exactly coincides with an individual SAR detection time.'
  }
  const durationHours = Math.abs(end - start) / 3600000
  const base = 'An AIS timestamp inside the SAR acquisition window receives full temporal compatibility; this indicates interval overlap, not exact coincidence with an individual SAR detection.'
  if (durationHours >= 12) {
    return `${base} This window spans ${durationHours.toFixed(1)} hours, so temporal compatibility has limited power to distinguish candidates within the window; their ranking is driven mainly by spatial fit.`
  }
  return base
}

function Phase3AssociationPanel({ startDate, endDate }: { startDate: string; endDate: string }) {
  const [status, setStatus] = useState<Phase3Status | null>(null)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [records, setRecords] = useState<SarRecord[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [scenario, setScenario] = useState<Scenario>('STANDARD')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('ALL')
  const [region, setRegion] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<SarRecord | null>(null)
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [candidateTotal, setCandidateTotal] = useState(0)
  const [rankingDiagnostics, setRankingDiagnostics] = useState<RankingDiagnostics | null>(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [indexNote, setIndexNote] = useState('')
  const listRequest = useRef(0)
  const overviewRequest = useRef(0)
  const detailRequest = useRef(0)

  const loadStatus = useCallback(async () => {
    try {
      const response = await fetch(`${API}/api/phase3/status`, { cache: 'no-store' })
      if (!response.ok) throw new Error(`Phase 3 status returned HTTP ${response.status}`)
      setStatus(await response.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load Phase 3 status.')
    }
  }, [])

  const loadOverview = useCallback(async () => {
    if (!status?.ready) return
    const requestId = ++overviewRequest.current
    try {
      const params = new URLSearchParams()
      if (startDate) params.set('start_date', startDate)
      if (endDate) params.set('end_date', endDate)
      const response = await fetch(`${API}/api/phase3/overview?${params}`)
      if (!response.ok) throw new Error(`Phase 3 overview returned HTTP ${response.status}`)
      const data: Overview = await response.json()
      if (requestId === overviewRequest.current) setOverview(data)
    } catch (e) {
      if (requestId === overviewRequest.current) setError(e instanceof Error ? e.message : 'Could not load Phase 3 overview.')
    }
  }, [status?.ready, startDate, endDate])

  const loadRecords = useCallback(async () => {
    if (!status?.ready) return
    const requestId = ++listRequest.current
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(page * PAGE_SIZE), association_status: statusFilter })
      if (startDate) params.set('start_date', startDate)
      if (endDate) params.set('end_date', endDate)
      if (region) params.set('region_iso3', region)
      if (search) params.set('search', search)
      const response = await fetch(`${API}/api/phase3/sar-records?${params}`)
      if (!response.ok) throw new Error(`Phase 3 SAR records returned HTTP ${response.status}`)
      const data = await response.json()
      if (requestId !== listRequest.current) return
      setRecords(data.observations || [])
      setTotal(data.total_matching || 0)
      setSelected(current => current ? (data.observations || []).find((r: SarRecord) => r.sar_record_id === current.sar_record_id) || null : null)
    } catch (e) {
      if (requestId === listRequest.current) setError(e instanceof Error ? e.message : 'Could not load Phase 3 SAR records.')
    } finally {
      if (requestId === listRequest.current) setLoading(false)
    }
  }, [status?.ready, startDate, endDate, page, statusFilter, region, search])

  const loadCandidates = useCallback(async (record: SarRecord, selectedScenario: Scenario) => {
    const requestId = ++detailRequest.current
    setDetailLoading(true)
    setDetailError('')
    setCandidates([])
    setCandidateTotal(0)
    setRankingDiagnostics(null)
    try {
      const params = new URLSearchParams({ scenario: selectedScenario, limit: '50' })
      const response = await fetch(`${API}/api/phase3/sar-records/${encodeURIComponent(record.sar_record_id)}/candidates?${params}`)
      if (!response.ok) throw new Error(`Candidate API returned HTTP ${response.status}`)
      const data = await response.json()
      if (requestId !== detailRequest.current) return
      setCandidates(data.candidates || [])
      setCandidateTotal(data.total_matching || 0)
      setRankingDiagnostics(data.ranking_diagnostics || null)
      if (data.index?.rebuilt) setIndexNote(`Built a local indexed candidate store with ${Number(data.index.rows_indexed || 0).toLocaleString('en-IN')} rows. Future candidate lookups will be faster.`)
      else setIndexNote('Local candidate index is ready for fast SAR-record lookups.')
    } catch (e) {
      if (requestId === detailRequest.current) setDetailError(e instanceof Error ? e.message : 'Could not load candidate details.')
    } finally {
      if (requestId === detailRequest.current) setDetailLoading(false)
    }
  }, [])

  useEffect(() => { void loadStatus() }, [loadStatus])
  useEffect(() => { void loadOverview() }, [loadOverview])
  useEffect(() => { setPage(0); setSelected(null); setCandidates([]) }, [startDate, endDate, statusFilter, region, search])
  useEffect(() => { void loadRecords() }, [loadRecords])
  useEffect(() => { if (selected) void loadCandidates(selected, scenario) }, [selected?.sar_record_id, scenario])

  const regions = useMemo(() => Object.keys(overview?.region_counts || {}).sort(), [overview?.region_counts])
  const maxPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1)
  const selectedSensitivity = overview?.sensitivity || []

  function chooseRecord(row: SarRecord) {
    setSelected(row)
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setPage(0)
    setSearch(searchInput.trim())
  }

  return (
    <section className="p3-panel">
      <div className="p3-head">
        <div>
          <div className="p3-eyebrow"><Database size={13} /> PHASE 3 · HIGH-RESOLUTION PILOT</div>
          <h2>SAR / AIS candidate intelligence</h2>
          <p>Independent space-time screening across 10 EEZ regions · 0.01° GFW grid-cell presence data</p>
        </div>
        <div className="p3-head-actions">
          <span className={`p3-ready ${status?.ready ? 'is-ready' : ''}`}>{status?.ready ? 'DATA READY' : 'AWAITING DATA FILES'}</span>
          <button className="p3-icon-btn" onClick={() => { void loadStatus(); void loadOverview(); void loadRecords() }} title="Refresh Phase 3 data"><RefreshCw size={15}/></button>
        </div>
      </div>

      {error && <div className="p3-error"><AlertTriangle size={15}/>{error}</div>}
      {status && !status.ready && (
        <div className="p3-waiting">
          <strong>Phase 3 result files have not been installed in the backend yet.</strong>
          <span>Copy the candidates, summary and sensitivity CSVs into <code>backend/data/processed/phase3/</code>. The existing dashboard will continue to work.</span>
          <span>Missing: {status.missing_files.join(', ')}</span>
        </div>
      )}

      {status?.ready && (
        <>
          <div className="p3-kpis">
            <div className="p3-kpi"><span>SAR PRESENCE RECORDS</span><strong>{(overview?.total_sar_presence_records ?? status.sar_presence_records ?? 0).toLocaleString('en-IN')}</strong><small>One summary row per SAR presence record</small></div>
            <div className="p3-kpi"><span>RECORDS WITH CANDIDATES</span><strong>{(overview?.records_with_candidates ?? 0).toLocaleString('en-IN')}</strong><small>{fmt(overview?.candidate_record_rate_pct, 2)}% screening availability</small></div>
            <div className="p3-kpi"><span>BROAD CANDIDATE PAIRS</span><strong>{(overview?.candidate_pairs_broad ?? status.candidate_pairs_broad ?? 0).toLocaleString('en-IN')}</strong><small>20 km / 180 min envelope</small></div>
            <div className="p3-kpi"><span>NO CANDIDATE IN ENVELOPE</span><strong>{(overview?.records_without_candidates ?? 0).toLocaleString('en-IN')}</strong><small>Does not establish vessel absence</small></div>
          </div>

          <div className="p3-sensitivity">
            <div className="p3-section-title"><Target size={15}/> Threshold sensitivity (screening only)</div>
            <div className="p3-sensitivity-grid">
              {selectedSensitivity.map(row => (
                <div className="p3-sensitivity-card" key={row.scenario}>
                  <b>{row.scenario}</b><strong>{Number(row.candidate_record_rate_pct || 0).toFixed(2)}%</strong>
                  <span>{Number(row.max_distance_km || 0)} km · up to {Number(row.max_time_gap_min || 0)} min outside-window gap</span>
                  <small>{Number(row.unique_sar_mmsi_candidate_pairs || 0).toLocaleString('en-IN')} candidate pairs</small>
                </div>
              ))}
            </div>
            <div className="p3-caution"><AlertTriangle size={14}/> Candidate record rate is not precision, recall, accuracy, calibrated probability or confirmed identity.</div>
          </div>

          <div className="p3-workspace">
            <div className="p3-records">
              <div className="p3-section-title"><MapPin size={15}/> SAR presence records</div>
              <div className="p3-filters">
                <form onSubmit={submitSearch} className="p3-search"><input value={searchInput} onChange={e => setSearchInput(e.target.value)} placeholder="Search record ID, region or MMSI…"/><button type="submit"><Search size={14}/></button></form>
                <select value={statusFilter} onChange={e => setStatusFilter(e.target.value as StatusFilter)} aria-label="Candidate status">
                  <option value="ALL">All statuses</option><option value="HAS_CANDIDATES">Has candidates</option><option value="NO_CANDIDATES">No candidates</option>
                </select>
                <select value={region} onChange={e => setRegion(e.target.value)} aria-label="Region">
                  <option value="">All regions</option>{regions.map(code => <option key={code} value={code}>{code}</option>)}
                </select>
              </div>
              <div className="p3-table-scroll">
                <table className="p3-table">
                  <thead><tr><th>REGION / DATE</th><th>CANDIDATES</th><th>NEAREST DISTANCE</th><th>STATUS</th></tr></thead>
                  <tbody>
                    {records.map(row => (
                      <tr key={row.sar_record_id} className={selected?.sar_record_id === row.sar_record_id ? 'selected' : ''} onClick={() => chooseRecord(row)}>
                        <td><b>{row.sar_region_iso3 || '—'}</b><small>{row.sar_date || '—'}</small></td>
                        <td>{Number(row.candidate_count_in_search_envelope || 0).toLocaleString('en-IN')}</td>
                        <td>{row.nearest_distance_km ? `${fmt(row.nearest_distance_km, 2)} km` : '—'}</td>
                        <td><span className={`p3-status ${row.association_status === 'independent_candidate_found' ? 'found' : 'none'}`}>{row.association_status === 'independent_candidate_found' ? 'CANDIDATES' : 'NO CANDIDATE'}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {loading && <div className="p3-empty">Loading SAR presence records…</div>}
                {!loading && records.length === 0 && <div className="p3-empty">No SAR records match these filters.</div>}
              </div>
              <div className="p3-pagination"><span>{total.toLocaleString('en-IN')} matching records · Page {page + 1} of {Math.max(1, maxPage + 1)}</span><div><button disabled={page === 0 || loading} onClick={() => setPage(v => Math.max(0, v - 1))}><ChevronLeft size={13}/></button><button disabled={page >= maxPage || loading} onClick={() => setPage(v => Math.min(maxPage, v + 1))}><ChevronRight size={13}/></button></div></div>
            </div>

            <div className="p3-detail">
              <div className="p3-detail-head"><div className="p3-section-title"><Ship size={15}/> Candidate review</div><select value={scenario} onChange={e => setScenario(e.target.value as Scenario)} aria-label="Matching scenario"><option value="STRICT">STRICT · 5 km / 30 min gap</option><option value="STANDARD">STANDARD · 10 km / 60 min gap</option><option value="BROAD">BROAD · 20 km / 180 min gap</option></select></div>
              {!selected && <div className="p3-empty">Select a SAR presence record to inspect its AIS candidates.</div>}
              {selected && (
                <>
                  <div className="p3-selected-record"><b>{selected.sar_region_label || selected.sar_region_iso3} · {selected.sar_date}</b><span>ID {selected.sar_record_id}</span><span>Grid centre: {fmt(selected.sar_lat, 5)}, {fmt(selected.sar_lon, 5)}</span><span>Source detections in cell: {selected.sar_source_detections || 'Not supplied'}</span><span>Acquisition: {selected.sar_entry_timestamp || '—'} to {selected.sar_exit_timestamp || '—'}</span><span>Window duration: {acquisitionDurationLabel(selected.sar_entry_timestamp, selected.sar_exit_timestamp)}</span></div>
                  <div className="p3-time-note"><b>How to read time:</b> {temporalWindowNote(selected)}</div>
                  {detailError && <div className="p3-error">{detailError}</div>}
                  {detailLoading ? <div className="p3-empty">Loading candidate details and preparing the local index if needed…</div> : (
                    <>
                      <div className="p3-candidate-count">{candidateTotal.toLocaleString('en-IN')} candidates under {scenario}</div>
                      {rankingDiagnostics?.top_candidate && (
                        <div className="p3-ranking-explain">
                          <div className="p3-ranking-explain-head"><Target size={14}/><b>Why the leading candidate ranks first</b></div>
                          <div className="p3-ranking-metrics">
                            <div><span>TOP RANK SCORE</span><strong>{fmt(rankingDiagnostics.top_candidate.scenario_ranking_score, 4)}</strong></div>
                            <div><span>DISTANCE CONTRIBUTION</span><strong>{fmt(rankingDiagnostics.top_candidate.distance_score_contribution, 3)} / 0.65</strong></div>
                            <div><span>WINDOW COMPATIBILITY CONTRIBUTION</span><strong>{fmt(rankingDiagnostics.top_candidate.time_score_contribution, 3)} / 0.35</strong></div>
                            <div><span>TOP-TWO SCORE GAP</span><strong>{rankingDiagnostics.top_two_score_gap == null ? '—' : fmt(rankingDiagnostics.top_two_score_gap, 4)}</strong></div>
                          </div>
                          <p>Distance and acquisition-window compatibility form the displayed screening score. An AIS timestamp inside the SAR window receives full temporal compatibility; this means interval overlap, not exact coincidence with an individual SAR detection. The top-two gap is a ranking-separation diagnostic, not identity confidence.</p>
                        </div>
                      )}
                      <div className="p3-candidate-list">
                        {candidates.map((candidate, i) => (
                          <div className="p3-candidate" key={`${candidate.candidate_mmsi}-${i}`}>
                            <div className="p3-candidate-rank">{i + 1}</div>
                            <div className="p3-candidate-main"><b>{candidate.candidate_ship_name || `MMSI ${candidate.candidate_mmsi}`}</b><span>MMSI {candidate.candidate_mmsi} · {candidate.candidate_region_iso3 || 'Region unknown'}</span><small>{candidate.candidate_vessel_type || 'Vessel type unavailable'} · {candidate.candidate_flag || 'Flag unavailable'}</small><small>Distance fit {fmt(candidate.distance_score_contribution, 3)} / 0.65 · Window fit {fmt(candidate.time_score_contribution, 3)} / 0.35</small><small>AIS presence: {candidate.candidate_observation_time || 'time unavailable'}</small></div>
                            <div className="p3-candidate-metrics"><b>{fmt(candidate.distance_km, 2)} km</b><span>{temporalLabel(candidate, selected)}</span><small>Scenario score {fmt(candidate.scenario_ranking_score, 3)}</small></div>
                          </div>
                        ))}
                        {candidates.length === 0 && <div className="p3-empty">No candidates meet this scenario's distance and time limits.</div>}
                      </div>
                      {indexNote && <div className="p3-footnote">{indexNote}</div>}
                    </>
                  )}
                </>
              )}
            </div>
          </div>
          <div className="p3-footnote">HIGH resolution means 0.01° GFW grid-cell presence data; coordinates still represent grid-cell centres, not raw vessel positions. Candidate ranking scores are not probabilities. An unmatched SAR presence record does not prove AIS absence or wrongdoing.</div>
        </>
      )}
    </section>
  )
}

export default Phase3AssociationPanel

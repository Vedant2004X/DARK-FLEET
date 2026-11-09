
import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import FleetMap from "./FleetMap";
import Phase3AssociationPanel from './Phase3AssociationPanel';
import RecentGridActivityPanel from './RecentGridActivityPanel';
import V09AnalyticsWorkspace from './V09AnalyticsWorkspace';
import {
  Activity,
  Anchor,
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Database,
  RefreshCw,
  Search,
  ShieldCheck,
  Ship,
  Waves,
  X,
} from 'lucide-react'

const API = 'http://127.0.0.1:8000'
const PAGE_SIZE = 100
const MAX_FEED_PAGES = 1000
const MAX_FEED_ROWS = PAGE_SIZE * MAX_FEED_PAGES

type Status = 'ALL' | 'REVIEW' | 'NORMAL'
type ApiState = 'loading' | 'connected' | 'offline'
type CacheBuildState = 'checking' | 'not_built' | 'building' | 'indexing' | 'ready' | 'error' | 'not_required'

type Observation = {
  mmsi: string
  date: string
  lat: string
  lon: string
  shipName?: string
  vesselType?: string
  speed_knots: string
  speed_change_knots: string
  time_gap_hours: string
  anomaly_score: string
  anomaly_flag: string
  anomaly_status?: string
  region_iso3?: string
  region_id?: string | number
  region_label?: string
  source_dataset?: string
}

type Summary = {
  total_observations: number
  flagged_observations: number
  baseline_observations: number
  unique_vessels: number
  source_file?: string
  data_mode?: string
  note?: string
}

type DataRangeResponse = {
  start_date: string
  end_date: string
  total_observations: number
  region_count: number
  source_file: string
  data_mode: string
  is_global: boolean
  note: string
  grid_totals?: { ais_grid_records: number; sar_grid_records: number }
  source_windows?: Array<{
    source: string
    label: string
    start_date: string | null
    end_date: string | null
    records?: number
    total_records?: number
    granularity: string
    identity_available: boolean
    temporal_windows?: Array<{ temporal_resolution: string; start_date: string; end_date: string; records: number }>
  }>
}

type ApiResponse = {
  total_matching: number
  total_returned: number
  observations: Observation[]
  note: string
}

type SarResponse = {
  total_matching: number
  total_returned?: number
  observations: Record<string, string>[]
  note: string
}

type MapObservationsResponse = {
  total_matching: number
  total_returned: number
  sampled_for_map?: boolean
  observations: Observation[]
  note: string
  source_file?: string
  data_mode?: string
}

type RecentGridOverviewResponse = {
  ais_grid_records: number
  sar_grid_records: number
  region_count: number
  start_date?: string | null
  end_date?: string | null
  note?: string
}

const emptySummary: Summary = {
  total_observations: 0,
  flagged_observations: 0,
  baseline_observations: 0,
  unique_vessels: 0,
}

function number(value: string | number | undefined, digits = 2) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : '—'
}

function App() {
  const [summary, setSummary] = useState<Summary>(emptySummary)
  const [observations, setObservations] = useState<Observation[]>([])
  const [mapObservations, setMapObservations] = useState<Observation[]>([])
  const [status, setStatus] = useState<Status>('ALL')
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [totalMatching, setTotalMatching] = useState(0)
  const [selected, setSelected] = useState<Observation | null>(null)
  const [apiState, setApiState] = useState<ApiState>('loading')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [apiNote, setApiNote] = useState('')
  const [showSar, setShowSar] = useState(false)
  const [activeNav, setActiveNav] = useState<
    'ocean' | 'command' | 'anomalies' | 'recent' | 'sar' | 'model'
  >('command')
  const mainTopRef = useRef<HTMLElement | null>(null)
  const mapWorkspaceRef = useRef<HTMLDivElement | null>(null)
  const anomalyQueueRef = useRef<HTMLDivElement | null>(null)
  const recentGridRef = useRef<HTMLDivElement | null>(null)
  const modelIntelligenceRef = useRef<HTMLDivElement | null>(null)
  const [mapSarRows, setMapSarRows] = useState<Record<string, string>[]>([])
  const [rangeReady, setRangeReady] = useState(false)
  const [rangeLoading, setRangeLoading] = useState(true)
  const [cacheBuildState, setCacheBuildState] = useState<CacheBuildState>('checking')
  const [cacheBuildRows, setCacheBuildRows] = useState(0)
  const [availableStartDate, setAvailableStartDate] = useState('')
  const [availableEndDate, setAvailableEndDate] = useState('')
  const [draftStartDate, setDraftStartDate] = useState('')
  const [draftEndDate, setDraftEndDate] = useState('')
  const [appliedStartDate, setAppliedStartDate] = useState('')
  const [appliedEndDate, setAppliedEndDate] = useState('')
  const [dateRangeError, setDateRangeError] = useState('')
  const [dateRangeNote, setDateRangeNote] = useState('')
  const [dataWindows, setDataWindows] = useState<DataRangeResponse['source_windows']>([])
  const [gridRefreshKey, setGridRefreshKey] = useState(0)
  const [mapObservationTotal, setMapObservationTotal] = useState(0)
  const [mapSarTotal, setMapSarTotal] = useState(0)
  const [recentGridOverview, setRecentGridOverview] = useState<RecentGridOverviewResponse | null>(null)
  const [recentGridOverviewLoading, setRecentGridOverviewLoading] = useState(false)
  const [recentGridOverviewError, setRecentGridOverviewError] = useState('')
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [mapAisLoading, setMapAisLoading] = useState(false)
  const [mapSarLoading, setMapSarLoading] = useState(false)
  const [mapAisError, setMapAisError] = useState('')
  const [mapSarError, setMapSarError] = useState('')

  // Ignore older API responses when a newer date range or refresh is already in flight.
  const summaryRequestRef = useRef(0)
  const observationsRequestRef = useRef(0)
  const mapObservationsRequestRef = useRef(0)
  const mapSarRequestRef = useRef(0)
  const recentGridOverviewRequestRef = useRef(0)
  const sarWorkspaceRef = useRef<HTMLDivElement | null>(null)
  const rangeSnapshotRef = useRef({ availableStartDate: '', availableEndDate: '', appliedStartDate: '', appliedEndDate: '' })
  rangeSnapshotRef.current = { availableStartDate, availableEndDate, appliedStartDate, appliedEndDate }

  const loadDataRange = useCallback(async (resetSelection = false) => {
    setRangeLoading(true)
    setCacheBuildState('checking')
    setCacheBuildRows(0)
    try {
      const statusResponse = await fetch(`${API}/api/cache-status`, { cache: 'no-store' })
      if (!statusResponse.ok) throw new Error(`Cache status API returned HTTP ${statusResponse.status}`)
      let cacheStatus = await statusResponse.json()

      if (cacheStatus.required && !cacheStatus.ready) {
        const prepareResponse = await fetch(`${API}/api/cache/prepare`, { method: 'POST', cache: 'no-store' })
        if (!prepareResponse.ok) throw new Error(`Cache preparation API returned HTTP ${prepareResponse.status}`)
        cacheStatus = await prepareResponse.json()
        const buildStartedAt = Date.now()
        while (!cacheStatus.ready) {
          if (cacheStatus.state === 'error') {
            throw new Error(cacheStatus.error || 'The local indexed data cache could not be built.')
          }
          if (Date.now() - buildStartedAt > 20 * 60 * 1000) {
            throw new Error('The local data index is taking longer than 20 minutes. Check /api/cache-status for details.')
          }
          setCacheBuildState(cacheStatus.state === 'indexing' ? 'indexing' : 'building')
          setCacheBuildRows(Number(cacheStatus.rows_loaded || 0))
          await new Promise((resolve) => window.setTimeout(resolve, 1500))
          const pollResponse = await fetch(`${API}/api/cache-status`, { cache: 'no-store' })
          if (!pollResponse.ok) throw new Error(`Cache status API returned HTTP ${pollResponse.status}`)
          cacheStatus = await pollResponse.json()
        }
      }
      setCacheBuildState(cacheStatus.state === 'not_required' ? 'not_required' : 'ready')
      setCacheBuildRows(Number(cacheStatus.rows_loaded || 0))

      const response = await fetch(`${API}/api/data-range`, { cache: 'no-store' })
      if (!response.ok) {
        const detail = await response.json().catch(() => null)
        throw new Error(detail?.detail?.message || `Data range API returned HTTP ${response.status}`)
      }
      const data: DataRangeResponse = await response.json()
      if (!data.start_date || !data.end_date) {
        throw new Error('The backend did not return an available date range.')
      }
      const previous = rangeSnapshotRef.current
      const hadSelection = Boolean(previous.appliedStartDate && previous.appliedEndDate)
      const wasFollowingLatest = hadSelection && previous.appliedEndDate === previous.availableEndDate
      const wasFollowingEarliest = hadSelection && previous.appliedStartDate === previous.availableStartDate
      const nextStart = resetSelection || !hadSelection || wasFollowingEarliest
        ? data.start_date
        : (previous.appliedStartDate < data.start_date ? data.start_date : previous.appliedStartDate)
      let nextEnd = resetSelection || !hadSelection || wasFollowingLatest ? data.end_date : previous.appliedEndDate
      if (nextEnd > data.end_date) nextEnd = data.end_date
      if (nextStart > nextEnd) {
        nextEnd = data.end_date
      }

      setAvailableStartDate(data.start_date)
      setAvailableEndDate(data.end_date)
      if (resetSelection || !hadSelection) {
        setDraftStartDate(nextStart)
        setDraftEndDate(nextEnd)
      } else {
        if (nextStart !== previous.appliedStartDate) setDraftStartDate(nextStart)
        if (nextEnd !== previous.appliedEndDate) setDraftEndDate(nextEnd)
      }
      setAppliedStartDate(nextStart)
      setAppliedEndDate(nextEnd)
      const newerAvailable = Boolean(previous.availableEndDate && data.end_date > previous.availableEndDate && !wasFollowingLatest)
      setDateRangeNote(newerAvailable
        ? `${data.note || ''} Newer source data is available through ${data.end_date}; click Reset range to follow the latest date.`
        : (data.note || ''))
      setDataWindows(data.source_windows || [])
      setRangeReady(true)
      // Refresh source-specific panels and their freshness status after each range poll.
      if (!resetSelection) setGridRefreshKey((value) => value + 1)
    } catch (err) {
      console.error('Could not load available data range:', err)
      setError(err instanceof Error ? err.message : 'Could not load available date range.')
      setApiState('offline')
    } finally {
      setRangeLoading(false)
    }
  }, [])

  const loadSummary = useCallback(async () => {
    if (!rangeReady || !appliedStartDate || !appliedEndDate) return
    const requestId = ++summaryRequestRef.current
    setSummaryLoading(true)
    try {
      const params = new URLSearchParams({
        start_date: appliedStartDate,
        end_date: appliedEndDate,
      })
      const response = await fetch(`${API}/api/model-summary?${params}`, { cache: 'no-store' })
      if (!response.ok) throw new Error(`Model summary API returned HTTP ${response.status}`)

      const data: Summary = await response.json()
      // A slower response for an older date range must never overwrite the current range.
      if (requestId !== summaryRequestRef.current) return
      setSummary(data)
      if (data.note) setApiNote(data.note)
      setApiState('connected')
    } catch (err) {
      if (requestId !== summaryRequestRef.current) return
      console.error('Could not load the model summary:', err)
      setError(err instanceof Error ? err.message : 'Could not load the model summary.')
    } finally {
      if (requestId === summaryRequestRef.current) setSummaryLoading(false)
    }
  }, [rangeReady, appliedStartDate, appliedEndDate])

  // Keep recent grid counts separate from historical vessel-level model metrics.
  // This makes the date filter's effect visible even when the historical model has no
  // records in a recent (for example, 2026) date window.
  const loadRecentGridOverview = useCallback(async () => {
    if (!rangeReady || !appliedStartDate || !appliedEndDate) return

    const requestId = ++recentGridOverviewRequestRef.current
    setRecentGridOverviewLoading(true)
    setRecentGridOverviewError('')

    try {
      const params = new URLSearchParams({
        start_date: appliedStartDate,
        end_date: appliedEndDate,
      })
      const response = await fetch(`${API}/api/recent-grid/overview?${params}`, {
        cache: 'no-store',
      })
      if (!response.ok) {
        throw new Error(`Recent grid overview returned HTTP ${response.status}`)
      }

      const data: RecentGridOverviewResponse = await response.json()
      if (requestId !== recentGridOverviewRequestRef.current) return
      setRecentGridOverview(data)
    } catch (err) {
      if (requestId !== recentGridOverviewRequestRef.current) return
      console.error('Could not load selected-range recent grid counts:', err)
      setRecentGridOverview(null)
      setRecentGridOverviewError(
        err instanceof Error ? err.message : 'Could not load recent grid counts.',
      )
    } finally {
      if (requestId === recentGridOverviewRequestRef.current) {
        setRecentGridOverviewLoading(false)
      }
    }
  }, [rangeReady, appliedStartDate, appliedEndDate])

  const loadObservations = useCallback(async () => {
    if (!rangeReady || !appliedStartDate || !appliedEndDate) return
    const requestId = ++observationsRequestRef.current
    setLoading(true)
    setError('')

    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(page * PAGE_SIZE),
        status,
        start_date: appliedStartDate,
        end_date: appliedEndDate,
      })
      if (search.trim()) params.set('search', search.trim())

      const response = await fetch(`${API}/api/anomalies?${params}`)
      if (!response.ok) throw new Error(`Anomaly API returned HTTP ${response.status}`)
      const data: ApiResponse = await response.json()
      if (requestId !== observationsRequestRef.current) return

      setObservations(data.observations || [])
      setTotalMatching(data.total_matching || 0)
      setApiNote(data.note || '')
      setSelected((current) => {
        if (current) {
          const exact = data.observations.find(
            (row) => row.mmsi === current.mmsi && row.date === current.date,
          )
          if (exact) return exact
        }
        return data.observations[0] ?? null
      })
      setApiState('connected')
    } catch (err) {
      if (requestId !== observationsRequestRef.current) return
      console.error('Could not load DARK-FLEET observations:', err)
      setError(err instanceof Error ? err.message : 'Could not load observations')
      setApiState('offline')
      setObservations([])
      setTotalMatching(0)
      setSelected(null)
    } finally {
      if (requestId === observationsRequestRef.current) setLoading(false)
    }
  }, [page, search, status, rangeReady, appliedStartDate, appliedEndDate])

  const loadMapObservations = useCallback(async () => {
    if (!rangeReady || !appliedStartDate || !appliedEndDate) return
    const requestId = ++mapObservationsRequestRef.current
    setMapAisLoading(true)
    setMapAisError('')
    setMapObservations([])
    setMapObservationTotal(0)
    try {
      const params = new URLSearchParams({
        limit: '6000',
        start_date: appliedStartDate,
        end_date: appliedEndDate,
      })
      const response = await fetch(`${API}/api/map-observations?${params}`)
      if (!response.ok) throw new Error(`Map observations API returned HTTP ${response.status}`)
      const data: MapObservationsResponse = await response.json()
      if (requestId !== mapObservationsRequestRef.current) return
      setMapObservations(data.observations || [])
      setMapObservationTotal(data.total_matching || 0)
      console.info(
        `DARK-FLEET map loaded ${data.total_returned} of ${data.total_matching} AIS points from ${data.source_file || 'map endpoint'}`,
      )
    } catch (err) {
      if (requestId !== mapObservationsRequestRef.current) return
      console.error('Could not load map observations:', err)
      setMapAisError(err instanceof Error ? err.message : 'Could not load map observations.')
      setMapObservations([])
      setMapObservationTotal(0)
    } finally {
      if (requestId === mapObservationsRequestRef.current) setMapAisLoading(false)
    }
  }, [rangeReady, appliedStartDate, appliedEndDate])

  const loadMapSar = useCallback(async () => {
    if (!rangeReady || !appliedStartDate || !appliedEndDate) return
    const requestId = ++mapSarRequestRef.current
    setMapSarLoading(true)
    setMapSarError('')
    setMapSarRows([])
    setMapSarTotal(0)
    try {
      const params = new URLSearchParams({
        limit: '6000',
        start_date: appliedStartDate,
        end_date: appliedEndDate,
      })
      const response = await fetch(`${API}/api/map-sar-detections?${params}`)
      if (!response.ok) throw new Error(`Map SAR API returned HTTP ${response.status}`)
      const data: SarResponse = await response.json()
      if (requestId !== mapSarRequestRef.current) return
      setMapSarRows(data.observations || [])
      setMapSarTotal(data.total_matching || 0)
      console.info(`DARK-FLEET map loaded ${data.total_returned ?? data.observations?.length ?? 0} SAR detections`)
    } catch (err) {
      if (requestId !== mapSarRequestRef.current) return
      console.error('Could not load map SAR detections:', err)
      setMapSarError(err instanceof Error ? err.message : 'Could not load SAR detections.')
      setMapSarRows([])
      setMapSarTotal(0)
    } finally {
      if (requestId === mapSarRequestRef.current) setMapSarLoading(false)
    }
  }, [rangeReady, appliedStartDate, appliedEndDate])


  useEffect(() => {
    void loadDataRange(true)
  }, [loadDataRange])

  // Poll the compact range/coverage metadata periodically. The ingestion worker updates
  // SQLite in the background; this advances the UI when new dates or records appear.
  useEffect(() => {
    if (!rangeReady) return
    const timer = window.setInterval(() => {
      void loadDataRange(false)
    }, 15 * 60 * 1000)
    return () => window.clearInterval(timer)
  }, [rangeReady, loadDataRange])

  useEffect(() => {
    if (!rangeReady) return
    void loadSummary()
  }, [rangeReady, loadSummary, gridRefreshKey])

  useEffect(() => {
    if (!rangeReady) return
    void loadRecentGridOverview()
  }, [rangeReady, loadRecentGridOverview, gridRefreshKey])

  useEffect(() => {
    if (!rangeReady) return
    void loadObservations()
  }, [rangeReady, loadObservations, gridRefreshKey])

  useEffect(() => {
    if (!rangeReady) return
    void loadMapObservations()
  }, [rangeReady, loadMapObservations, gridRefreshKey])

  useEffect(() => {
    if (!rangeReady) return
    void loadMapSar()
  }, [rangeReady, loadMapSar, gridRefreshKey])


  useEffect(() => {
    if (!showSar) return
    const frame = window.requestAnimationFrame(() => {
      sarWorkspaceRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [showSar])

  const maxPage = Math.min(Math.max(0, Math.ceil(totalMatching / PAGE_SIZE) - 1), MAX_FEED_PAGES - 1)
  const startRecord = totalMatching === 0 ? 0 : page * PAGE_SIZE + 1
  const endRecord = Math.min((page + 1) * PAGE_SIZE, totalMatching)

  function openCommandCenter() {
    setActiveNav('command')
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  function openOceanWatch() {
    setActiveNav('ocean')
    mapWorkspaceRef.current?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    })
  }

  function openAnomalyQueue() {
    setActiveNav('anomalies')
    changeStatus('REVIEW')
    window.requestAnimationFrame(() => {
      anomalyQueueRef.current?.scrollIntoView({
        behavior: 'smooth',
        block: 'start',
      })
    })
  }

  function openRecentGridWorkspace() {
    setActiveNav('recent')
    recentGridRef.current?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    })
  }

  function openModelIntelligenceWorkspace() {
    setActiveNav('model')
    modelIntelligenceRef.current?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    })
  }

  function openSarWorkspace() {
    setActiveNav('sar')
    if (showSar) {
      sarWorkspaceRef.current?.scrollIntoView({
        behavior: 'smooth',
        block: 'start',
      })
      return
    }
    setShowSar(true)
  }

  function closeSarWorkspace() {
    setShowSar(false)
    setActiveNav('command')
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  function changeStatus(nextStatus: Status) {
    setStatus(nextStatus)
    setPage(0)
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setPage(0)
    setSearch(searchInput.trim())
  }

  function clearSearch() {
    setSearchInput('')
    setSearch('')
    setPage(0)
  }

  function applyDateRange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!draftStartDate || !draftEndDate) {
      setDateRangeError('Choose both a start date and an end date.')
      return
    }
    if (draftStartDate > draftEndDate) {
      setDateRangeError('The start date must be on or before the end date.')
      return
    }
    if (draftStartDate < availableStartDate || draftEndDate > availableEndDate) {
      setDateRangeError(`Choose dates between ${availableStartDate} and ${availableEndDate}.`)
      return
    }
    setDateRangeError('')
    setAppliedStartDate(draftStartDate)
    setAppliedEndDate(draftEndDate)
    setPage(0)
  }

  function resetDateRange() {
    setDateRangeError('')
    setDraftStartDate(availableStartDate)
    setDraftEndDate(availableEndDate)
    setAppliedStartDate(availableStartDate)
    setAppliedEndDate(availableEndDate)
    setPage(0)
  }

  return (
    <div className="df-app">
      <style>{`
        * { box-sizing: border-box; }
        body { margin: 0; background: #07111d; }
        button, input { font: inherit; }
        button { cursor: pointer; }
        button:disabled { cursor: not-allowed; opacity: .45; }
        .df-app {
          min-height: 100vh; background: #07111d; color: #e8f0f8;
          font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
        }
        .df-layout { min-height: 100vh; }
        .df-sidebar {
          position: fixed; top: 0; left: 0; bottom: 0;
          width: 230px; height: 100vh; height: 100dvh;
          overflow-y: auto; overscroll-behavior: contain;
          scrollbar-width: thin; scrollbar-color: #29465b #0a1624;
          z-index: 100;
          background: #0a1624; border-right: 1px solid #1a2a3c;
          padding: 24px 16px; display: flex; flex-direction: column; gap: 24px;
        }
        .df-brand { display: flex; align-items: center; gap: 11px; padding: 0 5px; }
        .df-logo {
          width: 42px; height: 42px; display: grid; place-items: center;
          background: #12394a; border: 1px solid #24677b; border-radius: 12px; color: #6ce3f2;
        }
        .df-brand-name { font-weight: 850; letter-spacing: 1.6px; font-size: 18px; }
        .df-brand-name span { color: #6ce3f2; }
        .df-subtitle { color: #7c92a8; font-size: 9px; letter-spacing: 1.4px; margin-top: 4px; }
        .df-section-label { color: #71879e; font-size: 10px; letter-spacing: 1.7px; font-weight: 750; margin: 0 8px 10px; }
        .df-nav { display: grid; gap: 5px; }
        .df-nav button {
          color: #9eb0c3; border: 1px solid transparent; background: transparent;
          border-radius: 9px; padding: 11px 10px; text-align: left; display: flex;
          align-items: center; gap: 10px; font-size: 12px;
        }
        .df-nav button.active { color: #dffaff; background: #123044; border-color: #21485d; }
        .df-nav button:hover { background: #112638; }
        .df-nav button:focus-visible {
          outline: 2px solid #6ce3f2; outline-offset: 2px;
        }
        .df-side-bottom { margin-top: auto; }
        .df-system { padding: 13px; border: 1px solid #20354a; border-radius: 12px; background: #0d1c2c; }
        .df-system-title { font-size: 10px; color: #8ca2b8; letter-spacing: 1px; }
        .df-online { color: #6de0b0; font-weight: 800; font-size: 11px; margin-top: 8px; }
        .df-dot { display: inline-block; width: 7px; height: 7px; background: #6de0b0; border-radius: 50%; margin-right: 7px; }
        .df-main { min-width: 0; margin-left: 230px; }
        .df-topbar {
          min-height: 66px; border-bottom: 1px solid #1a2a3c; display: flex;
          align-items: center; justify-content: space-between; padding: 14px 28px; gap: 16px;
        }
        .df-crumb { color: #8095aa; font-size: 12px; }
        .df-crumb strong { color: #dce7f1; font-weight: 650; }
        .df-build { color: #e9c875; font-size: 9px; letter-spacing: 1.2px; border: 1px solid #51452b; padding: 5px 7px; border-radius: 5px; margin-left: 12px; }
        .df-top-status { font-size: 11px; color: #91a7bc; }
        .df-content { max-width: 1600px; margin: auto; padding: 30px 28px 50px; }
        .df-heading { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; margin-bottom: 25px; }
        .df-eyebrow { font-size: 10px; color: #6ce3f2; letter-spacing: 2px; font-weight: 800; }
        .df-heading h1 { margin: 10px 0 8px; font-size: clamp(28px,3vw,38px); letter-spacing: -1.4px; }
        .df-heading h1 em { color: #6ce3f2; font-style: normal; }
        .df-heading p { margin: 0; font-size: 13px; color: #849ab0; }
        .df-actions { display: flex; gap: 9px; flex-wrap: wrap; }
        .df-btn {
          border: 1px solid #294157; background: #102235; color: #dce9f5;
          padding: 10px 13px; border-radius: 8px; font-size: 11px; font-weight: 700;
          display: inline-flex; align-items: center; gap: 7px;
        }
        .df-btn:hover { background: #173148; }
        .df-btn-primary { background: #155165; border-color: #277d91; color: #e6fcff; }
        .df-btn-primary:hover { background: #1b657a; }
        .df-metrics { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 13px; margin-bottom: 22px; }
        .df-card { background: #0c1a29; border: 1px solid #1d3044; border-radius: 12px; }
        .df-metric { padding: 17px; min-width: 0; }
        .df-metric-head { display: flex; justify-content: space-between; align-items: center; color: #8da4ba; font-size: 10px; letter-spacing: .7px; gap: 8px; }
        .df-metric-icon { color: #6ce3f2; }
        .df-metric-value { font-size: clamp(23px,2.6vw,30px); font-weight: 800; letter-spacing: -1px; margin: 13px 0 5px; overflow-wrap: anywhere; }
        .df-metric-note { color: #849ab0; font-size: 10px; }
        .df-grid { display: grid; grid-template-columns: minmax(0,1.7fr) minmax(270px,1fr); gap: 17px; align-items: start; }
        .df-panel-head { padding: 17px 18px; border-bottom: 1px solid #1c3044; display: flex; justify-content: space-between; align-items: center; gap: 10px; }
        .df-panel-title { font-size: 13px; font-weight: 800; }
        .df-panel-sub { color: #8299af; font-size: 10px; margin-top: 5px; }
        .df-panel-body { padding: 16px 18px; }
        .df-filters { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 13px; }
        .df-filter {
          padding: 8px 11px; border: 1px solid #263b50; background: #0b1927;
          border-radius: 7px; color: #92a8bc; font-size: 11px;
        }
        .df-filter.active { background: #164253; color: #c9f9ff; border-color: #347386; }
        .df-search { display: flex; gap: 7px; margin-bottom: 15px; }
        .df-search input {
          min-width: 0; flex: 1; border: 1px solid #253b50; background: #081522;
          border-radius: 8px; color: #e8f0f8; padding: 10px 12px; outline: none; font-size: 12px;
        }
        .df-search input:focus { border-color: #3a8798; }
        .df-table-wrap { overflow-x: auto; }
        .df-table { width: 100%; border-collapse: collapse; min-width: 540px; }
        .df-table th { color: #71889e; font-size: 9px; text-align: left; font-weight: 750; letter-spacing: .8px; padding: 11px 9px; border-bottom: 1px solid #24374a; }
        .df-table td { padding: 12px 9px; font-size: 11px; color: #b8c8d8; border-bottom: 1px solid #182b3d; white-space: nowrap; }
        .df-table tbody tr { cursor: pointer; }
        .df-table tbody tr:hover, .df-table tbody tr.selected { background: #10283a; }
        .df-table .primary-text { color: #e4eff9; font-weight: 700; }
        .df-status { display: inline-flex; align-items: center; border-radius: 5px; padding: 5px 7px; font-size: 9px; font-weight: 800; }
        .df-status.review { color: #ffd58b; background: #3b2c18; border: 1px solid #654b25; }
        .df-status.normal { color: #8be4ba; background: #123328; border: 1px solid #245a43; }
        .df-pagination { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding-top: 14px; color: #8da3b8; font-size: 10px; }
        .df-page-buttons { display: flex; gap: 6px; }
        .df-page-buttons button { background: #102235; border: 1px solid #294157; border-radius: 7px; color: #dce9f5; padding: 7px 9px; display: flex; align-items: center; gap: 4px; font-size: 10px; }
        .df-detail { padding: 18px; }
        .df-detail-ship { display: flex; align-items: center; gap: 12px; margin: 15px 0 20px; }
        .df-ship-icon { width: 43px; height: 43px; display: grid; place-items: center; border-radius: 11px; color: #6ce3f2; background: #123448; }
        .df-detail-name { font-size: 15px; font-weight: 800; overflow-wrap: anywhere; }
        .df-detail-id { color: #8097ad; font-size: 10px; margin-top: 5px; }
        .df-detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
        .df-detail-item { background: #091624; border: 1px solid #1c3043; border-radius: 8px; padding: 11px; min-width: 0; }
        .df-detail-label { color: #8198ad; font-size: 9px; letter-spacing: .7px; }
        .df-detail-value { margin-top: 8px; font-size: 15px; font-weight: 750; overflow-wrap: anywhere; }
        .df-note { color: #90a5b9; font-size: 11px; line-height: 1.65; margin-top: 15px; padding: 12px; background: #091624; border-radius: 8px; }
        .df-section { margin-top: 18px; }
        .df-sar-workspace { display: grid; gap: 12px; margin: 20px 0 24px; scroll-margin-top: 14px; }
        .df-sar-workspace-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 14px 16px; border: 1px solid #20384a; border-radius: 12px; background: linear-gradient(110deg,#102638,#0c1a29); }
        .df-sar-workspace-kicker { color: #6ce3f2; font-size: 9px; font-weight: 850; letter-spacing: 1.3px; }
        .df-sar-workspace-head h2 { margin: 5px 0 3px; color: #e8f1f8; font-size: 16px; }
        .df-sar-workspace-head p { margin: 0; color: #91a9bd; font-size: 10px; line-height: 1.5; }
        @media(max-width:680px) { .df-sar-workspace-head { align-items: flex-start; flex-direction: column; } }
        .df-alert { border: 1px solid #624c28; background: #2a2418; color: #f4d9a0; padding: 12px 14px; border-radius: 9px; font-size: 11px; line-height: 1.6; margin-bottom: 16px; }
        .df-error { border: 1px solid #6c383b; background: #2b191e; color: #ffc2c2; padding: 12px; border-radius: 8px; font-size: 12px; margin-bottom: 14px; }
        .df-empty { text-align: center; padding: 35px 10px; color: #879db2; font-size: 12px; }
        .df-sar-table { overflow-x: auto; }
        .df-footer { color: #6e8499; font-size: 10px; line-height: 1.6; margin-top: 20px; }
        .df-date-panel { margin: 0 0 20px; padding: 16px; border: 1px solid #20384a; border-radius: 12px; background: linear-gradient(135deg,#0d1e2d,#0b1927); }
        .df-date-panel-heading { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }
        .df-date-eyebrow { color: #6ce3f2; font-size: 9px; font-weight: 800; letter-spacing: 1.5px; }
        .df-date-panel h2 { margin: 5px 0; font-size: 15px; }
        .df-date-panel p { margin: 0; color: #8ea5b9; font-size: 11px; line-height: 1.5; }
        .df-date-source { color: #a8bdce; background: #102638; border: 1px solid #24465b; border-radius: 7px; padding: 7px 9px; font-size: 10px; }
        .df-date-form { display: flex; align-items: end; flex-wrap: wrap; gap: 10px; }
        .df-date-form label { display: grid; gap: 6px; min-width: 160px; }
        .df-date-form label span { color: #9db2c5; font-size: 10px; font-weight: 700; }
        .df-date-form input[type="date"] { min-height: 38px; padding: 8px 10px; border: 1px solid #294258; border-radius: 8px; color: #e8f0f8; background: #081522; color-scheme: dark; outline: none; }
        .df-date-form input[type="date"]:focus { border-color: #4b9fb0; }
        .df-date-footnote { margin-top: 10px; color: #8299af; font-size: 10px; line-height: 1.6; overflow-wrap: anywhere; }
        .df-source-windows { display: grid; gap: 4px; margin-top: 7px; }
        .df-source-windows span { display: block; color: #91a9bd; }
        .df-source-windows strong { color: #b9d9e5; }
        .df-grid-range-summary {
          margin-top: 13px; padding: 12px 13px;
          border: 1px solid #20384a; border-radius: 9px;
          background: #091827;
        }
        .df-grid-range-title {
          color: #6ce3f2; font-size: 9px; font-weight: 800;
          letter-spacing: 1.2px; margin-bottom: 9px;
        }
        .df-grid-range-stats {
          display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 10px;
        }
        .df-grid-range-stat { min-width: 0; }
        .df-grid-range-stat span {
          display: block; color: #8299af; font-size: 9px;
          text-transform: uppercase; letter-spacing: .5px;
        }
        .df-grid-range-stat strong {
          display: block; color: #e8f4fa; font-size: 18px;
          font-weight: 800; margin-top: 4px; overflow-wrap: anywhere;
        }
        .df-grid-range-note {
          margin-top: 8px; color: #8096aa; font-size: 9px; line-height: 1.5;
        }
        .df-grid-range-error { color: #ffc2c2; font-size: 10px; }
        .df-navigation-target { scroll-margin-top: 18px; }
        .df-content { scroll-margin-top: 0; }
        @media(max-width:1050px) {
          .df-sidebar { width: 190px; }
          .df-main { margin-left: 190px; }
          .df-content { padding: 22px 18px; }
          .df-metrics { grid-template-columns: repeat(2,minmax(0,1fr)); }
          .df-grid { grid-template-columns: 1fr; }
        }
        @media(max-width:680px) {
          .df-sidebar { display: none; }
          .df-main { margin-left: 0; }
          .df-topbar { padding: 13px 16px; }
          .df-content { padding: 20px 12px; }
          .df-heading { flex-direction: column; }
          .df-metrics { gap: 8px; }
          .df-metric { padding: 12px; }
          .df-metric-head { font-size: 9px; }
          .df-metric-note { font-size: 9px; }
          .df-build { display: none; }
          .df-date-form { align-items: stretch; }
          .df-date-form label { min-width: 100%; }
          .df-date-form button { flex: 1; justify-content: center; }
        }
      `}</style>

      <div className="df-layout">
        <aside className="df-sidebar">
          <div className="df-brand">
            <div className="df-logo"><RadarIcon /></div>
            <div>
              <div className="df-brand-name">DARK<span>FLEET</span></div>
              <div className="df-subtitle">MARITIME INTELLIGENCE</div>
            </div>
          </div>

          <div>
            <div className="df-section-label">WORKSPACE</div>
            <div className="df-nav">
              <button
                type="button"
                className={activeNav === 'ocean' ? 'active' : ''}
                aria-current={activeNav === 'ocean' ? 'page' : undefined}
                onClick={openOceanWatch}
              >
                <Waves size={16} /> Ocean Watch
              </button>
            </div>
          </div>

          <div>
            <div className="df-section-label">OPERATIONS</div>
            <div className="df-nav">
              <button
                type="button"
                className={activeNav === 'command' ? 'active' : ''}
                aria-current={activeNav === 'command' ? 'page' : undefined}
                onClick={openCommandCenter}
              >
                <Activity size={16} /> Command center
              </button>
              <button
                type="button"
                className={activeNav === 'anomalies' ? 'active' : ''}
                aria-current={activeNav === 'anomalies' ? 'page' : undefined}
                onClick={openAnomalyQueue}
              >
                <AlertTriangle size={16} /> Anomaly queue
              </button>
              <button
                type="button"
                className={activeNav === 'recent' ? 'active' : ''}
                aria-current={activeNav === 'recent' ? 'page' : undefined}
                onClick={openRecentGridWorkspace}
              >
                <Waves size={16} /> Recent grid activity
              </button>
              <button
                type="button"
                className={activeNav === 'model' ? 'active' : ''}
                aria-current={activeNav === 'model' ? 'page' : undefined}
                onClick={openModelIntelligenceWorkspace}
              >
                <ShieldCheck size={16} /> Model intelligence
              </button>
              <button
                type="button"
                className={activeNav === 'sar' ? 'active' : ''}
                aria-current={activeNav === 'sar' ? 'page' : undefined}
                onClick={openSarWorkspace}
              >
                <Anchor size={16} /> SAR/AIS comparison
              </button>
            </div>
          </div>

          <div className="df-side-bottom">
            <div className="df-system">
              <div className="df-system-title">SYSTEM STATUS</div>
              <div className="df-online">
                <span className="df-dot" style={{ background: apiState === 'connected' ? '#6de0b0' : apiState === 'offline' ? '#ff7777' : '#e9c875' }} />
                {apiState === 'connected' ? 'API ONLINE' : apiState === 'offline' ? 'API OFFLINE' : 'CONNECTING'}
              </div>
              <div className="df-panel-sub">Sources: historical model + recent grid database</div>
            </div>
          </div>
        </aside>

        <main className="df-main" ref={mainTopRef}>
          <header className="df-topbar">
            <div className="df-crumb">
              Operations&nbsp; / &nbsp;<strong>Command center</strong>
              <span className="df-build">RESEARCH BUILD · v0.9.0</span>
            </div>
            <div className="df-top-status">
              <span className="df-dot" style={{ background: apiState === 'connected' ? '#6de0b0' : '#ff7777' }} />
              {apiState === 'connected' ? 'API CONNECTED' : apiState === 'offline' ? 'API OFFLINE' : 'CHECKING API'}
            </div>
          </header>

          <div className="df-content">
            <div className="df-heading">
              <div>
                <div className="df-eyebrow">— MARITIME DOMAIN AWARENESS</div>
                <h1>Command <em>center.</em></h1>
                <p>Historical vessel-anomaly analysis · Recent AIS/SAR grid activity</p>
              </div>
              <div className="df-actions">
                <button className="df-btn" onClick={() => { void loadDataRange(false) }} disabled={loading || rangeLoading}>
                  <RefreshCw size={14} /> {loading || rangeLoading ? 'Refreshing…' : 'Refresh data'}
                </button>
                <button className="df-btn" onClick={openRecentGridWorkspace}>
                  <Waves size={14} /> Recent activity
                </button>
                <button className="df-btn df-btn-primary" onClick={openSarWorkspace}>
                  <Anchor size={14} /> SAR/AIS results
                </button>
              </div>
            </div>

            <section className="df-date-panel" aria-label="Observation date range">
              <div className="df-date-panel-heading">
                <div>
                  <div className="df-date-eyebrow">DATA WINDOW</div>
                  <h2>Unified data date range</h2>
                  <p>Applies to historical vessel-level outputs and recent grid activity. Source-specific metrics remain separate; dates without collected records remain empty.</p>
                </div>
                <span className="df-date-source">
                  {rangeLoading
                    ? (cacheBuildState === 'indexing'
                      ? `Finalizing local database indexes · ${cacheBuildRows.toLocaleString('en-IN')} rows loaded…`
                      : cacheBuildState === 'building'
                        ? `Preparing local indexed data · ${cacheBuildRows.toLocaleString('en-IN')} rows processed…`
                        : 'Checking local data cache…')
                    : recentGridOverviewLoading
                      ? 'Updating recent AIS/SAR counts…'
                      : recentGridOverview
                        ? `${(recentGridOverview.ais_grid_records + recentGridOverview.sar_grid_records).toLocaleString('en-IN')} recent grid records in range`
                        : summaryLoading
                          ? 'Updating historical model metrics…'
                          : 'Source-specific data counts'}
                </span>
              </div>
              <form className="df-date-form" onSubmit={applyDateRange}>
                <label>
                  <span>Start date</span>
                  <input
                    type="date"
                    value={draftStartDate}
                    min={availableStartDate || undefined}
                    max={availableEndDate || undefined}
                    onChange={(event) => setDraftStartDate(event.target.value)}
                    disabled={!rangeReady}
                    required
                  />
                </label>
                <label>
                  <span>End date</span>
                  <input
                    type="date"
                    value={draftEndDate}
                    min={availableStartDate || undefined}
                    max={availableEndDate || undefined}
                    onChange={(event) => setDraftEndDate(event.target.value)}
                    disabled={!rangeReady}
                    required
                  />
                </label>
                <button className="df-btn df-btn-primary" type="submit" disabled={!rangeReady || rangeLoading || (draftStartDate === appliedStartDate && draftEndDate === appliedEndDate)}>
                  Apply dates
                </button>
                <button className="df-btn" type="button" onClick={resetDateRange} disabled={!rangeReady || (draftStartDate === availableStartDate && draftEndDate === availableEndDate && appliedStartDate === availableStartDate && appliedEndDate === availableEndDate)}>
                  Reset range
                </button>
              </form>
              <div className="df-date-footnote">
                Unified calendar: {availableStartDate || '—'} through {availableEndDate || '—'}. {dateRangeNote || 'Date filters include only records present in the collected sources; this is not live tracking.'}
                {dataWindows?.length ? <div className="df-source-windows">{dataWindows.map((window) => <span key={window.source}><strong>{window.label}:</strong> {window.start_date || '—'} to {window.end_date || '—'} · {window.granularity}</span>)}</div> : null}
              </div>

              <div className="df-grid-range-summary" aria-live="polite">
                <div className="df-grid-range-title">RECENT GRID ACTIVITY · SELECTED DATE RANGE</div>
                {recentGridOverviewLoading ? (
                  <div className="df-date-footnote">Updating recent AIS/SAR records for {appliedStartDate} through {appliedEndDate}…</div>
                ) : recentGridOverviewError ? (
                  <div className="df-grid-range-error">{recentGridOverviewError}</div>
                ) : recentGridOverview ? (
                  <>
                    <div className="df-grid-range-stats">
                      <div className="df-grid-range-stat">
                        <span>AIS grid records</span>
                        <strong>{recentGridOverview.ais_grid_records.toLocaleString('en-IN')}</strong>
                      </div>
                      <div className="df-grid-range-stat">
                        <span>SAR grid records</span>
                        <strong>{recentGridOverview.sar_grid_records.toLocaleString('en-IN')}</strong>
                      </div>
                      <div className="df-grid-range-stat">
                        <span>Regions represented</span>
                        <strong>{recentGridOverview.region_count.toLocaleString('en-IN')}</strong>
                      </div>
                    </div>
                    <div className="df-grid-range-note">
                      {appliedStartDate} through {appliedEndDate}. Grid-cell-hour aggregates grouped by flag; these are not individual vessel identities or anomaly scores.
                    </div>
                  </>
                ) : (
                  <div className="df-date-footnote">Recent grid counts are not available for this date range.</div>
                )}
              </div>

              {dateRangeError && <div className="df-error" role="alert">{dateRangeError}</div>}
            </section>

            {error && <div className="df-error">{error}</div>}

            <div className="df-metrics">
              <Metric icon={<Database size={17} />} label="HISTORICAL OBSERVATIONS SCORED" value={summary.total_observations} note="Selected date range" loading={summaryLoading} />
              <Metric icon={<AlertTriangle size={17} />} label="HISTORICAL FLAGGED FOR REVIEW" value={summary.flagged_observations} note="Requires human review" loading={summaryLoading} />
              <Metric icon={<Ship size={17} />} label="HISTORICAL UNIQUE MMSI" value={summary.unique_vessels} note="Selected date range" loading={summaryLoading} />
              <Metric icon={<Activity size={17} />} label="HISTORICAL BASELINE OBSERVATIONS" value={summary.baseline_observations} note="Model baseline" loading={summaryLoading} />
            </div>

            <div className="df-alert">
              <strong>Research caution:</strong> Isolation Forest flags are statistical indicators, not proof of illegal activity. AIS/SAR proximity results are provisional and require time, location, coverage and data-quality checks.
            </div>

            <div
              ref={mapWorkspaceRef}
              id="ocean-watch"
              className="df-navigation-target"
            >
              <FleetMap
                observations={mapObservations}
                sarDetections={mapSarRows}
                dateRangeLabel={`${appliedStartDate} — ${appliedEndDate}`}
                observationTotal={mapObservationTotal}
                sarTotal={mapSarTotal}
                loading={mapAisLoading || mapSarLoading}
                error={mapAisError || mapSarError}
                onSelectObservation={(item) => {
                  const match = observations.find(
                    (row) => row.mmsi === item.mmsi && row.date === item.date,
                  )
                  setSelected(match ?? {
                    mmsi: item.mmsi ?? "UNKNOWN",
                    date: item.date ?? "",
                    lat: String(item.lat ?? ""),
                    lon: String(item.lon ?? ""),
                    shipName: item.shipName,
                    vesselType: item.vesselType,
                    speed_knots: String(item.speed_knots ?? ""),
                    speed_change_knots: String(item.speed_change_knots ?? ""),
                    time_gap_hours: String(item.time_gap_hours ?? ""),
                    anomaly_score: String(item.anomaly_score ?? ""),
                    anomaly_flag: String(item.anomaly_flag ?? "0"),
                    anomaly_status: item.anomaly_status ?? "Map observation",
                    region_iso3: item.region_iso3,
                    region_id: item.region_id,
                    region_label: item.region_label,
                    source_dataset: item.source_dataset,
                  })
                }}
              />
            </div>

            <div ref={recentGridRef} id="recent-grid-activity" style={{ scrollMarginTop: 16 }}>
              <RecentGridActivityPanel startDate={appliedStartDate} endDate={appliedEndDate} rangeReady={rangeReady} refreshKey={gridRefreshKey} />
            </div>

            <div ref={modelIntelligenceRef} id="model-intelligence" style={{ scrollMarginTop: 16 }}>
              <V09AnalyticsWorkspace
                startDate={appliedStartDate}
                endDate={appliedEndDate}
                rangeReady={rangeReady}
                refreshKey={gridRefreshKey}
              />
            </div>

            {showSar && (
              <div ref={sarWorkspaceRef} id="sar-ais-workspace" className="df-sar-workspace">
                <div className="df-sar-workspace-head">
                  <div>
                    <div className="df-sar-workspace-kicker">MARITIME INTELLIGENCE</div>
                    <h2>SAR / AIS comparison workspace</h2>
                    <p>High-resolution SAR/AIS candidate screening and explainable review across the 10-region pilot.</p>
                  </div>
                  <button className="df-btn" onClick={closeSarWorkspace}><X size={13} /> Close workspace</button>
                </div>
              <Phase3AssociationPanel startDate={appliedStartDate} endDate={appliedEndDate} />


              </div>
            )}

            <div
              ref={anomalyQueueRef}
              id="anomaly-queue"
              className="df-grid df-navigation-target"
            >
              <section className="df-card">
                <div className="df-panel-head">
                  <div>
                    <div className="df-panel-title">Observation feed</div>
                    <div className="df-panel-sub">Selected date range · Mixed flagged/baseline order · Search · Pagination</div>
                  </div>
                  <span className="df-status normal">{apiState === 'connected' ? 'API CONNECTED' : apiState.toUpperCase()}</span>
                </div>

                <div className="df-panel-body">
                  <div className="df-filters">
                    {(['ALL', 'REVIEW', 'NORMAL'] as Status[]).map((item) => (
                      <button
                        key={item}
                        className={`df-filter ${status === item ? 'active' : ''}`}
                        onClick={() => changeStatus(item)}
                      >
                        {item === 'ALL' ? 'All observations' : item === 'REVIEW' ? 'Flagged' : 'Baseline'}
                        {item === 'REVIEW' && ` (${summary.flagged_observations.toLocaleString('en-IN')})`}
                        {item === 'NORMAL' && ` (${summary.baseline_observations.toLocaleString('en-IN')})`}
                      </button>
                    ))}
                  </div>
                  {status === 'ALL' && (
                    <div className="df-date-footnote" style={{ marginTop: -2, marginBottom: 10 }}>
                      Showing both classes in mixed order while preserving their actual dataset proportions. Use Flagged or Baseline to isolate a class.
                    </div>
                  )}

                  <form className="df-search" onSubmit={submitSearch}>
                    <input
                      value={searchInput}
                      onChange={(event) => setSearchInput(event.target.value)}
                      placeholder="Search MMSI, vessel name, or date…"
                      aria-label="Search observations"
                    />
                    <button className="df-btn" type="submit"><Search size={14} /> Search</button>
                    {search && <button className="df-btn" type="button" onClick={clearSearch} aria-label="Clear search"><X size={14} /></button>}
                  </form>

                  <div className="df-table-wrap">
                    <table className="df-table">
                      <thead>
                        <tr>
                          <th>VESSEL / MMSI</th>
                          <th>LAST OBSERVED</th>
                          <th>SPEED</th>
                          <th>ANOMALY SCORE</th>
                          <th>STATUS</th>
                        </tr>
                      </thead>
                      <tbody>
                        {observations.map((row, index) => {
                          const flagged = row.anomaly_flag === '-1'
                          const isSelected = selected?.mmsi === row.mmsi && selected?.date === row.date
                          return (
                            <tr
                              key={`${row.mmsi}-${row.date}-${index}`}
                              className={isSelected ? 'selected' : ''}
                              onClick={() => setSelected(row)}
                            >
                              <td>
                                <div className="primary-text">{row.shipName?.trim() || `MMSI ${row.mmsi}`}</div>
                                <div className="df-panel-sub">{row.mmsi}</div>
                              </td>
                              <td>{row.date || '—'}</td>
                              <td>{number(row.speed_knots, 1)} kn</td>
                              <td>{number(row.anomaly_score, 4)}</td>
                              <td><span className={`df-status ${flagged ? 'review' : 'normal'}`}>{flagged ? 'FLAGGED' : 'BASELINE'}</span></td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                    {loading && <div className="df-empty">Loading observations…</div>}
                    {!loading && observations.length === 0 && <div className="df-empty">No observations found for this filter and search.</div>}
                  </div>

                  <div className="df-pagination">
                    <span>Showing {startRecord.toLocaleString('en-IN')}–{endRecord.toLocaleString('en-IN')} of {totalMatching.toLocaleString('en-IN')}</span>
                    <div className="df-page-buttons">
                      <button disabled={page === 0 || loading} onClick={() => setPage((value) => Math.max(0, value - 1))}><ChevronLeft size={13} /> Previous</button>
                      <button disabled={page >= maxPage || loading} onClick={() => setPage((value) => Math.min(maxPage, value + 1))}>Next <ChevronRight size={13} /></button>
                    </div>
                  </div>
                  {totalMatching > MAX_FEED_ROWS && (
                    <div className="df-date-footnote">
                      Pagination is capped at the first {MAX_FEED_ROWS.toLocaleString('en-IN')} newest matching records to protect memory use. Narrow the date range or search by vessel to explore other records.
                    </div>
                  )}
                </div>
              </section>

              <section className="df-card">
                <div className="df-panel-head">
                  <div>
                    <div className="df-panel-title">Observation details</div>
                    <div className="df-panel-sub">Select a row to inspect its features</div>
                  </div>
                  <Ship size={17} color="#6ce3f2" />
                </div>

                {selected ? (
                  <div className="df-detail">
                    <div className="df-detail-ship">
                      <div className="df-ship-icon"><Ship size={22} /></div>
                      <div>
                        <div className="df-detail-name">{selected.shipName?.trim() || `MMSI ${selected.mmsi}`}</div>
                        <div className="df-detail-id">MMSI {selected.mmsi}</div>
                      </div>
                    </div>

                    <span className={`df-status ${selected.anomaly_flag === '-1' ? 'review' : 'normal'}`}>
                      {selected.anomaly_flag === '-1' ? 'FLAGGED FOR REVIEW' : 'BASELINE OBSERVATION'}
                    </span>

                    <div className="df-detail-grid" style={{ marginTop: 15 }}>
                      <Detail label="LATITUDE" value={number(selected.lat, 5)} />
                      <Detail label="LONGITUDE" value={number(selected.lon, 5)} />
                      <Detail label="SPEED" value={`${number(selected.speed_knots, 2)} kn`} />
                      <Detail label="SPEED CHANGE" value={`${number(selected.speed_change_knots, 2)} kn`} />
                      <Detail label="TIME GAP" value={`${number(selected.time_gap_hours, 1)} h`} />
                      <Detail label="ANOMALY SCORE" value={number(selected.anomaly_score, 5)} />
                    </div>

                    <div className="df-note">
                      <strong>Observation time</strong><br />
                      {selected.date || 'Not available'}
                      {(selected.region_label || selected.region_iso3) && (
                        <>
                          <br /><br />
                          <strong>Region</strong><br />
                          {selected.region_label || selected.region_iso3}
                        </>
                      )}
                      {selected.source_dataset && (
                        <>
                          <br /><br />
                          <strong>Source dataset</strong><br />
                          {selected.source_dataset}
                        </>
                      )}
                      <br /><br />
                      <strong>Interpretation</strong><br />
                      {selected.anomaly_flag === '-1'
                        ? 'This observation was flagged by the unsupervised model. Review its context and data quality before drawing conclusions.'
                        : 'This observation is part of the model baseline. Baseline status does not guarantee normal or safe activity.'}
                    </div>
                  </div>
                ) : (
                  <div className="df-empty">Select an observation to see details.</div>
                )}
              </section>
            </div>

            <div className="df-footer">
              DARK-FLEET · Research prototype · AIS-derived hourly observations and unsupervised anomaly scores.
              <br />
              {apiNote}
              <br />
              Results are preliminary research outputs, not verified maritime incidents or proof of wrongdoing.
            </div>
          </div>
        </main>
      </div>
    </div>
  )
}

function Metric({
  icon,
  label,
  value,
  note,
  loading = false,
}: {
  icon: ReactNode
  label: string
  value: number
  note: string
  loading?: boolean
}) {
  return (
    <div className="df-card df-metric">
      <div className="df-metric-head">
        <span>{label}</span>
        <span className="df-metric-icon">{icon}</span>
      </div>
      <div className="df-metric-value">{loading ? 'Updating…' : value.toLocaleString('en-IN')}</div>
      <div className="df-metric-note">{note}</div>
    </div>
  )
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="df-detail-item">
      <div className="df-detail-label">{label}</div>
      <div className="df-detail-value">{value}</div>
    </div>
  )
}

function RadarIcon() {
  return <Activity size={23} />
}

export default App
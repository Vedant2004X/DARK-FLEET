import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, AlertCircle, CheckCircle2, Database, MapPin, RefreshCw, Satellite, Waves } from 'lucide-react'
import { CircleMarker, MapContainer, Popup, TileLayer, useMap } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'
import './RecentGridActivityPanel.css'

const API = 'http://127.0.0.1:8000'

type RecentStatus = {
  state: string
  ready: boolean
  files_ready: boolean
  missing_files?: string[]
  rows_loaded?: number
  ais_rows_loaded?: number
  sar_rows_loaded?: number
  error?: string
  files?: Record<string, { exists: boolean; size_mb: number }>
  invalid_ais_rows?: number
  invalid_sar_rows?: number
  last_ingestion?: { safe_end_date_exclusive_utc?: string; safe_lag_days?: number; successful_reports_this_run?: number; failed_reports_this_run?: number }
}

type RecentOverview = {
  ais_grid_records: number
  sar_grid_records: number
  start_date: string | null
  end_date: string | null
  region_count: number
  region_counts: Array<{ region_iso3: string; kind: string; n: number }>
  note: string
}

type GridPoint = {
  kind: 'ais' | 'sar'
  day: string
  date_text: string
  lat: number
  lon: number
  flag: string
  hours?: number | null
  vessel_ids?: number | null
  detections?: number | null
  region_iso3: string
  region_label?: string
  spatial_resolution?: string
  temporal_resolution?: string
  source_dataset?: string
}

type GridMapResponse = {
  ais_points: GridPoint[]
  sar_points: GridPoint[]
  total_ais_matching: number
  total_sar_matching: number
  total_returned: number
  sampled_for_map: boolean
  note: string
}

type SarRecordResponse = { records: GridPoint[]; total_matching: number }

function formatCount(value: number | undefined) {
  return Number(value || 0).toLocaleString('en-IN')
}

function MapFit({ points }: { points: GridPoint[] }) {
  const map = useMap()
  useEffect(() => {
    if (!points.length) return
    const bounds = points.map((point) => [point.lat, point.lon] as [number, number])
    if (bounds.length === 1) map.setView(bounds[0], 7)
    else map.fitBounds(bounds, { padding: [28, 28], maxZoom: 7 })
  }, [map, points])
  return null
}

function RecentGridMap({ ais, sar, showAis, showSar }: {
  ais: GridPoint[]; sar: GridPoint[]; showAis: boolean; showSar: boolean
}) {
  const points = useMemo(() => [
    ...(showAis ? ais : []),
    ...(showSar ? sar : []),
  ], [ais, sar, showAis, showSar])

  return (
    <div className="rg-map-wrap">
      {!points.length && <div className="rg-map-empty">No valid grid records for these filters.</div>}
      <MapContainer center={[10, 35]} zoom={2} scrollWheelZoom className="rg-map">
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        <MapFit points={points} />
        {showAis && ais.map((point, index) => (
          <CircleMarker
            key={`recent-ais-${point.region_iso3}-${point.date_text}-${point.lat}-${point.lon}-${index}`}
            center={[point.lat, point.lon]}
            radius={5}
            pathOptions={{ color: '#083344', weight: 1, fillColor: '#22c5d9', fillOpacity: 0.78 }}
          >
            <Popup>
              <strong>Recent AIS grid presence</strong><br />
              Date: {point.date_text}<br />
              Region: {point.region_iso3}<br />
              Flag: {point.flag || 'Not reported'}<br />
              Hours: {point.hours ?? '—'}<br />
              Aggregate vesselIDs count in cell: {point.vessel_ids ?? '—'}<br />
              Grid centre: {point.lat.toFixed(4)}, {point.lon.toFixed(4)}<br />
              Time aggregation: {point.temporal_resolution || '—'}<br />
              <em>Aggregate grid-cell activity; not an individual vessel position.</em>
            </Popup>
          </CircleMarker>
        ))}
        {showSar && sar.map((point, index) => (
          <CircleMarker
            key={`recent-sar-${point.region_iso3}-${point.date_text}-${point.lat}-${point.lon}-${index}`}
            center={[point.lat, point.lon]}
            radius={7}
            pathOptions={{ color: '#451a03', weight: 1, fillColor: '#f59e0b', fillOpacity: 0.88 }}
          >
            <Popup>
              <strong>Recent SAR grid presence</strong><br />
              Date: {point.date_text}<br />
              Region: {point.region_iso3}<br />
              Detections in cell: {point.detections ?? '—'}<br />
              Aggregate vesselIDs count in cell: {point.vessel_ids ?? '—'}<br />
              Grid centre: {point.lat.toFixed(4)}, {point.lon.toFixed(4)}<br />
              Time aggregation: {point.temporal_resolution || '—'}<br />
              <em>Aggregated satellite presence; not a confirmed vessel identity.</em>
            </Popup>
          </CircleMarker>
        ))}
      </MapContainer>
    </div>
  )
}

export default function RecentGridActivityPanel({ startDate, endDate, rangeReady, refreshKey }: { startDate: string; endDate: string; rangeReady: boolean; refreshKey: number }) {
  const [status, setStatus] = useState<RecentStatus | null>(null)
  const [overview, setOverview] = useState<RecentOverview | null>(null)
  const [aisPoints, setAisPoints] = useState<GridPoint[]>([])
  const [sarPoints, setSarPoints] = useState<GridPoint[]>([])
  const [sarRows, setSarRows] = useState<GridPoint[]>([])
  const [region, setRegion] = useState('ALL')
  const [showAis, setShowAis] = useState(true)
  const [showSar, setShowSar] = useState(true)
  const [loading, setLoading] = useState(false)
  const [building, setBuilding] = useState(false)
  const [error, setError] = useState('')

  const loadStatus = useCallback(async () => {
    const response = await fetch(`${API}/api/recent-grid/status`, { cache: 'no-store' })
    if (!response.ok) throw new Error(`Recent grid status returned HTTP ${response.status}`)
    const data: RecentStatus = await response.json()
    setStatus(data)
    return data
  }, [])

  useEffect(() => {
    void loadStatus().catch((err) => setError(err instanceof Error ? err.message : 'Could not check recent grid files.'))
  }, [loadStatus, refreshKey])

  const loadRecentData = useCallback(async (start: string, end: string, selectedRegion: string) => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams({ limit: '5000' })
      const overviewParams = new URLSearchParams()
      if (start) { params.set('start_date', start); overviewParams.set('start_date', start) }
      if (end) { params.set('end_date', end); overviewParams.set('end_date', end) }
      if (selectedRegion !== 'ALL') { params.set('region_iso3', selectedRegion); overviewParams.set('region_iso3', selectedRegion) }
      const [overviewResponse, mapResponse, sarResponse] = await Promise.all([
        fetch(`${API}/api/recent-grid/overview?${overviewParams}`, { cache: 'no-store' }),
        fetch(`${API}/api/recent-grid/map?${params}`, { cache: 'no-store' }),
        fetch(`${API}/api/recent-grid/records?kind=sar&limit=10${start ? `&start_date=${start}` : ''}${end ? `&end_date=${end}` : ''}${selectedRegion !== 'ALL' ? `&region_iso3=${selectedRegion}` : ''}`, { cache: 'no-store' }),
      ])
      if (!overviewResponse.ok) throw new Error(`Recent grid overview returned HTTP ${overviewResponse.status}`)
      if (!mapResponse.ok) throw new Error(`Recent grid map returned HTTP ${mapResponse.status}`)
      if (!sarResponse.ok) throw new Error(`Recent SAR feed returned HTTP ${sarResponse.status}`)
      const [overviewData, mapData, sarData] = await Promise.all([
        overviewResponse.json() as Promise<RecentOverview>,
        mapResponse.json() as Promise<GridMapResponse>,
        sarResponse.json() as Promise<SarRecordResponse>,
      ])
      setOverview(overviewData)
      setAisPoints(mapData.ais_points || [])
      setSarPoints(mapData.sar_points || [])
      setSarRows(sarData.records || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load recent grid activity.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (status?.ready && rangeReady && startDate && endDate) {
      void loadRecentData(startDate, endDate, region)
    }
  }, [status?.ready, rangeReady, startDate, endDate, region, refreshKey, loadRecentData])

  async function prepareIndex() {
    setBuilding(true)
    setError('')
    try {
      const response = await fetch(`${API}/api/recent-grid/prepare`, { method: 'POST', cache: 'no-store' })
      if (!response.ok) {
        const detail = await response.json().catch(() => null)
        throw new Error(detail?.detail?.message || `Index preparation returned HTTP ${response.status}`)
      }
      let current: RecentStatus = await response.json()
      setStatus(current)
      while (!current.ready) {
        if (current.state === 'error') throw new Error(current.error || 'Recent grid index build failed.')
        await new Promise((resolve) => window.setTimeout(resolve, 1500))
        current = await loadStatus()
      }
      setStatus(current)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not prepare recent grid index.')
      await loadStatus().catch(() => undefined)
    } finally {
      setBuilding(false)
    }
  }

  const regions = useMemo(() => Array.from(new Set((overview?.region_counts || []).map((r) => r.region_iso3))).sort(), [overview])
  const indexing = building || status?.state === 'building'

  return (
    <section className="recent-grid-panel" aria-label="Historical and recent grid activity">
      <div className="recent-grid-head">
        <div>
          <div className="recent-grid-kicker"><Activity size={13} /> AIS / SAR GRID ACTIVITY · HIGH RESOLUTION</div>
          <h2>Maritime grid activity</h2>
          <p>Historical backfill and latest available hourly AIS/SAR grid activity across the 10 pilot EEZ regions. This dataset remains separate from vessel-level anomaly outputs.</p>
        </div>
        <span className={`recent-grid-badge ${status?.ready ? 'ready' : 'pending'}`}>
          {status?.ready ? 'INDEX READY' : indexing ? 'INDEXING' : status?.files_ready ? 'DATA AVAILABLE' : 'DATA FILES NEEDED'}
        </span>
      </div>

      {!status?.files_ready && (
        <div className="recent-grid-state">
          <AlertCircle size={18} />
          <div>
            <strong>Recent grid files not found on this computer.</strong>
            <p>Copy <code>recent_grid_ais_presence.csv</code> and <code>recent_grid_sar_presence.csv</code> into <code>backend/data/processed/recent_grid/</code>, then refresh this panel.</p>
            {status?.missing_files?.length ? <small>Missing: {status.missing_files.join(', ')}</small> : null}
          </div>
          <button className="rg-btn" onClick={() => void loadStatus().catch((err) => setError(String(err)))}><RefreshCw size={13} /> Check files</button>
        </div>
      )}

      {status?.files_ready && !status.ready && !indexing && (
        <div className="recent-grid-state">
          <Database size={18} />
          <div>
            <strong>Build the local recent-data index</strong>
            <p>The source CSVs are preserved. DARK-FLEET will create a compact indexed SQLite database for fast date, region and map queries. This first preparation runs once per source-file version.</p>
            <small>AIS source: {status.files?.['recent_grid_ais_presence.csv']?.size_mb?.toFixed(1) ?? '—'} MB · SAR source: {status.files?.['recent_grid_sar_presence.csv']?.size_mb?.toFixed(1) ?? '—'} MB</small>
          </div>
          <button className="rg-btn primary" onClick={() => void prepareIndex()}><Database size={13} /> Build index</button>
        </div>
      )}

      {indexing && (
        <div className="recent-grid-build">
          <div className="recent-grid-build-title"><span>Preparing recent-data index…</span><strong>{formatCount(status?.rows_loaded)} rows processed</strong></div>
          <div className="recent-grid-progress"><span /></div>
          <small>This is a one-time local indexing step. Keep the backend terminal running.</small>
        </div>
      )}

      {status?.ready && overview && (
        <>
          <div className="recent-grid-metrics">
            <div><span>AIS GRID RECORDS</span><strong>{formatCount(overview.ais_grid_records)}</strong><small>FLAG-grouped hourly cells</small></div>
            <div><span>SAR GRID RECORDS</span><strong>{formatCount(overview.sar_grid_records)}</strong><small>FLAG-grouped hourly cells</small></div>
            <div><span>REGIONS</span><strong>{formatCount(overview.region_count)}</strong><small>Collected EEZ regions</small></div>
            <div><span>AVAILABLE WINDOW</span><strong>{overview.start_date || '—'} → {overview.end_date || '—'}</strong><small>Records present in the selected date range</small></div>
          </div>

          <div className="recent-grid-controls">
            <div className="rg-shared-range"><span>Shared dashboard date range</span><strong>{startDate || '—'} → {endDate || '—'}</strong><small>Change dates in the unified data window above.</small></div>
            <label><span>Region</span><select value={region} onChange={(e) => setRegion(e.target.value)}><option value="ALL">All regions</option>{regions.map((r) => <option key={r} value={r}>{r}</option>)}</select></label>
            <span className="rg-loading-label">{loading ? 'Updating recent data…' : 'Region filter applies to grid activity only.'}</span>
          </div>

          {error && <div className="recent-grid-error" role="alert">{error}</div>}

          <div className="recent-grid-map-heading">
            <div><h3><MapPin size={15} /> Recent grid activity map</h3><p>Sampled map points: {formatCount(aisPoints.length)} AIS · {formatCount(sarPoints.length)} SAR</p></div>
            <div className="recent-grid-toggles">
              <label><input type="checkbox" checked={showAis} onChange={(e) => setShowAis(e.target.checked)} /><i className="rg-dot ais" /> AIS grid</label>
              <label><input type="checkbox" checked={showSar} onChange={(e) => setShowSar(e.target.checked)} /><i className="rg-dot sar" /> SAR grid</label>
            </div>
          </div>
          <RecentGridMap ais={aisPoints} sar={sarPoints} showAis={showAis} showSar={showSar} />

          <div className="recent-grid-sar-table">
            <div className="recent-grid-map-heading"><div><h3><Satellite size={15} /> Latest SAR grid presence records</h3><p>Recent SAR presence samples for the selected range</p></div></div>
            <div className="rg-table-wrap">
              <table><thead><tr><th>DATE (UTC)</th><th>REGION</th><th>FLAG</th><th>DETECTIONS</th><th>VESSEL COUNT IN CELL</th><th>GRID CENTRE</th></tr></thead>
                <tbody>{sarRows.map((row, i) => <tr key={`${row.region_iso3}-${row.date_text}-${row.lat}-${row.lon}-${i}`}><td>{row.date_text}</td><td>{row.region_iso3}</td><td>{row.flag || '—'}</td><td>{row.detections ?? '—'}</td><td>{row.vessel_ids ?? '—'}</td><td>{row.lat.toFixed(3)}, {row.lon.toFixed(3)}</td></tr>)}</tbody>
              </table>
              {!sarRows.length && <div className="rg-no-rows">No SAR grid records for the selected filters.</div>}
            </div>
          </div>
          <p className="recent-grid-caveat">Interpretation: these are HIGH-resolution grid-cell aggregates grouped by FLAG. Historical backfill and recent refresh rows are HOURLY. AIS <code>vesselIDs</code> and SAR <code>vesselIDs</code> are aggregate cell counts, not individual MMSI identities. These grid records are not passed into the historical Isolation Forest or Phase 3 vessel-candidate engine.</p>
        </>
      )}
      {error && !status?.ready && <div className="recent-grid-error" role="alert">{error}</div>}
      {status?.ready && <div className="recent-grid-source"><CheckCircle2 size={13} /> Local indexed data ready · HIGH resolution · HOURLY historical + recent · FLAG grouped · Identity unavailable{status.last_ingestion?.safe_lag_days ? ` · ${status.last_ingestion.safe_lag_days}-day source safety lag` : ''}{status.last_ingestion?.safe_end_date_exclusive_utc ? ` · Ingested through ${status.last_ingestion.safe_end_date_exclusive_utc} (exclusive)` : ''}</div>}
    </section>
  )
}


import { useEffect, useMemo, useState } from "react";
import {
  CircleMarker,
  MapContainer,
  Popup,
  TileLayer,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

type FleetObservation = {
  mmsi?: string;
  shipName?: string;
  vesselType?: string;
  lat?: string | number;
  lon?: string | number;
  date?: string;
  speed_knots?: string | number;
  speed_change_knots?: string | number;
  time_gap_hours?: string | number;
  anomaly_score?: string | number;
  anomaly_flag?: string | number;
  anomaly_status?: string;
  region_iso3?: string;
  region_id?: string | number;
  region_label?: string;
  source_dataset?: string;
};

type SarDetection = {
  lat?: string | number;
  lon?: string | number;
  sar_lat?: string | number;
  sar_lon?: string | number;
  date?: string;
  sar_time?: string;
  shipName?: string;
  mmsi?: string;
  nearest_mmsi?: string;
  nearest_ship?: string;
  distance_km?: string | number;
  time_diff_min?: string | number;
  association_status?: string;
  entryTimestamp?: string;
  detections?: string | number;
  region_iso3?: string;
  region_label?: string;
};

type FleetMapProps = {
  observations: FleetObservation[];
  sarDetections: SarDetection[];
  dateRangeLabel?: string;
  observationTotal?: number;
  sarTotal?: number;
  loading?: boolean;
  error?: string;
  onSelectObservation?: (observation: FleetObservation) => void;
};

function FitMap({ points }: { points: [number, number][] }) {
  const map = useMap();

  useEffect(() => {
    if (points.length === 1) {
      map.setView(points[0], 7);
    } else if (points.length > 1) {
      map.fitBounds(L.latLngBounds(points), {
        padding: [35, 35],
        maxZoom: 9,
      });
    }
  }, [map, points]);

  return null;
}

function validCoordinate(
  lat: unknown,
  lon: unknown,
): [number, number] | null {
  if (lat == null || lon == null) return null;
  if (String(lat).trim() === "" || String(lon).trim() === "") return null;

  const latitude = Number(lat);
  const longitude = Number(lon);

  if (
    !Number.isFinite(latitude) ||
    !Number.isFinite(longitude) ||
    latitude < -90 ||
    latitude > 90 ||
    longitude < -180 ||
    longitude > 180
  ) {
    return null;
  }

  return [latitude, longitude];
}

function isFlagged(item: FleetObservation): boolean {
  return (
    String(item.anomaly_flag) === "-1" ||
    item.anomaly_status === "Flagged for review"
  );
}

export default function FleetMap({
  observations,
  sarDetections,
  dateRangeLabel,
  observationTotal = observations.length,
  sarTotal = sarDetections.length,
  loading = false,
  error = "",
  onSelectObservation,
}: FleetMapProps) {
  const [showFlagged, setShowFlagged] = useState(true);
  const [showBaseline, setShowBaseline] = useState(true);
  const [showSarMarkers, setShowSarMarkers] = useState(true);

  // Filter AIS observations according to the selected controls.
  const visibleObservations = useMemo(
    () =>
      observations.filter((item) =>
        isFlagged(item) ? showFlagged : showBaseline,
      ),
    [observations, showFlagged, showBaseline],
  );

  // Keep the SAR array stable between renders.
  const visibleSarDetections = useMemo(
    () => (showSarMarkers ? sarDetections : []),
    [showSarMarkers, sarDetections],
  );

  // Fit the map to the coordinates of visible markers only.
  const points = useMemo(() => {
    const all: [number, number][] = [];

    visibleObservations.forEach((item) => {
      const point = validCoordinate(item.lat, item.lon);
      if (point) all.push(point);
    });

    visibleSarDetections.forEach((item) => {
      const point = validCoordinate(
        item.sar_lat ?? item.lat,
        item.sar_lon ?? item.lon,
      );
      if (point) all.push(point);
    });

    return all;
  }, [visibleObservations, visibleSarDetections]);

  return (
    <section className="fleet-map-panel">
      <div className="fleet-map-heading">
        <div>
          <h2>Geospatial Activity</h2>
          <p>
            {loading
              ? `Loading map data for ${dateRangeLabel || "the selected date range"}…`
              : `${dateRangeLabel ? `Date range: ${dateRangeLabel}. ` : ""}Showing ${observations.length.toLocaleString("en-IN")} sampled AIS points from ${observationTotal.toLocaleString("en-IN")} matching records, and ${sarDetections.length.toLocaleString("en-IN")} SAR points from ${sarTotal.toLocaleString("en-IN")} matching detections.`}
          </p>
        </div>

        <div className="fleet-map-legend">
          <span>
            <i className="legend-dot flagged" /> Flagged AIS
          </span>
          <span>
            <i className="legend-dot baseline" /> Baseline AIS
          </span>
          <span>
            <i className="legend-dot sar" /> Nearby AIS candidate
          </span>
          <span>
            <i className="legend-dot sar-no-nearby" /> No nearby AIS candidate
          </span>
          <span>
            <i className="legend-dot sar-unverified" /> SAR not yet compared
          </span>
        </div>
      </div>

      {error && <div className="fleet-map-error" role="alert">Map data error: {error}</div>}

      {/* Independent visibility controls */}
      <div className="fleet-map-filters" aria-label="Map marker filters">
        <label>
          <input
            type="checkbox"
            checked={showFlagged}
            onChange={(event) => setShowFlagged(event.target.checked)}
          />
          <span className="legend-dot flagged" />
          Flagged AIS
        </label>

        <label>
          <input
            type="checkbox"
            checked={showBaseline}
            onChange={(event) => setShowBaseline(event.target.checked)}
          />
          <span className="legend-dot baseline" />
          Baseline AIS
        </label>

        <label>
          <input
            type="checkbox"
            checked={showSarMarkers}
            onChange={(event) => setShowSarMarkers(event.target.checked)}
          />
          <span className="legend-dot sar" />
          SAR detections
        </label>
      </div>

      {!loading && observations.length === 0 && sarDetections.length === 0 && (
        <p className="fleet-map-state" role="status">
          No valid AIS or SAR coordinates were returned for this date range. Try a wider date range.
        </p>
      )}
      {!loading && observations.length + sarDetections.length > 0 &&
        visibleObservations.length + visibleSarDetections.length === 0 && (
          <p className="fleet-map-state" role="status">
            All marker layers are hidden by the map filters. Turn on at least one layer to display points.
          </p>
        )}

      <MapContainer
        center={[-48, -74]}
        zoom={5}
        scrollWheelZoom
        className="fleet-leaflet-map"
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />

        <FitMap points={points} />

        {/* AIS markers: orange = flagged, green = baseline */}
        {visibleObservations.map((item, index) => {
          const point = validCoordinate(item.lat, item.lon);
          if (!point) return null;

          const flagged = isFlagged(item);
          const markerColor = flagged ? "#f59e0b" : "#34d399";

          return (
            
            <CircleMarker
            key={`ais-${item.mmsi}-${item.date}-${index}`}
            center={point}
            radius={flagged ? 8 : 6}
            pathOptions={{
                stroke: true,
                color: "#0f172a",
                fillColor: markerColor,
                fillOpacity: 1,
                opacity: 1,
                weight: 2,
            }}
            eventHandlers={{
                click: () => onSelectObservation?.(item),
            }}
            >

              <Popup>
                <strong>{item.shipName || "Unknown vessel"}</strong>
                <br />
                MMSI: {item.mmsi || "Unavailable"}
                <br />
                Status: {flagged ? "Flagged for review" : "Baseline"}
                <br />
                Speed: {item.speed_knots ?? "N/A"} knots
                <br />
                Speed change: {item.speed_change_knots ?? "N/A"} knots
                <br />
                Time gap: {item.time_gap_hours ?? "N/A"} hours
                <br />
                Anomaly score: {item.anomaly_score ?? "N/A"}
                <br />
                Observed: {item.date || "Unavailable"}
                <br />
                Region: {item.region_label || item.region_iso3 || "Unavailable"}
                <br />
                Position: {point[0].toFixed(4)}, {point[1].toFixed(4)}
                <br />
                <em>
                  An anomaly flag is a statistical signal for review, not
                  proof of wrongdoing.
                </em>
              </Popup>
            </CircleMarker>
          );
        })}

        {/* SAR markers: blue = nearby candidate, pink = no nearby candidate */}
        {visibleSarDetections.map((item, index) => {
          const point = validCoordinate(
            item.sar_lat ?? item.lat,
            item.sar_lon ?? item.lon,
          );

          if (!point) return null;

            const associationStatus = (item.association_status ?? "")
            .trim()
            .toLowerCase();

            const hasNearbyCandidate =
            associationStatus === "nearby_ais_candidate";

            const noNearbyCandidate =
            associationStatus === "no_nearby_ais_candidate";

            const statusLabel = hasNearbyCandidate
            ? "Nearby AIS candidate"
            : noNearbyCandidate
                ? "No nearby AIS candidate"
                : "Association not verified";

            const markerColor = hasNearbyCandidate
            ? "#168bff"
            : noNearbyCandidate
                ? "#ff3b81"
                : "#a78bfa";


          return (
            
            <CircleMarker
            key={`sar-${item.nearest_mmsi}-${item.sar_time}-${index}`}
            center={point}
            radius={9}
            pathOptions={{
                stroke: true,
                color: "#0f172a",
                fillColor: markerColor,
                fillOpacity: 1,
                opacity: 1,
                weight: 2,
            }}
            >

              <Popup>
                <strong>SAR vessel detection</strong>
                <br />
                <strong>Status: {statusLabel}</strong>
                <br />
                Detection time: {item.sar_time || item.entryTimestamp || item.date || "Unavailable"}
                <br />
                Region: {item.region_label || item.region_iso3 || "Unavailable"}
                <br />
                Detection count: {item.detections ?? "Unavailable"}
                <br />
                Nearest AIS candidate: {item.nearest_ship || "Unavailable"}
                <br />
                Candidate MMSI: {item.nearest_mmsi || "Unavailable"}
                <br />
                Distance: {item.distance_km ?? "Unavailable"} km
                <br />
                Time difference: {item.time_diff_min ?? "Unavailable"} minutes
                <br />
                Position: {point[0].toFixed(4)}, {point[1].toFixed(4)}
                <br />
                <em>
                  Proximity does not confirm that the AIS candidate is the
                  detected vessel. No nearby candidate does not prove illegal
                  activity.
                </em>
              </Popup>
            </CircleMarker>
          );
        })}
      </MapContainer>

      <p className="fleet-map-footnote">
        Markers represent sampled observations, not continuous vessel tracks.
        Anomaly flags and SAR/AIS proximity comparisons are provisional
        investigation signals, not proof of wrongdoing.
      </p>
    </section>
  );
}

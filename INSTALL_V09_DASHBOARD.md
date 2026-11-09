# DARK-FLEET v0.9 Dashboard — Group 2

This package adds a Model Intelligence workspace to the existing v0.8.5 dashboard and connects it to the read-only v0.9 API.

## Files included

Replace:
- `frontend/src/App.tsx` — complete updated application file, retaining the existing v0.8.5 dashboard workspaces and adding the Model Intelligence navigation item and workspace.

Add:
- `frontend/src/V09AnalyticsWorkspace.tsx` — new read-only analytics UI component.

No backend file, database, CSV, ingestion worker, or existing map/panel component is replaced.

## Install

1. From the DARK-FLEET project root, back up the current App.tsx:

   ```powershell
   Copy-Item .\frontend\src\App.tsx ".\frontend\src\App.tsx.backup_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
   ```

2. Extract this ZIP directly into the project root, preserving folder paths and allowing the included `frontend/src/App.tsx` to replace the existing file.

3. Confirm these paths exist:

   ```powershell
   Test-Path .\frontend\src\App.tsx
   Test-Path .\frontend\src\V09AnalyticsWorkspace.tsx
   ```

   Both should return `True`.

4. Build the frontend from its folder:

   ```powershell
   Push-Location .\frontend
   & "C:\Program Files\nodejs\npm.cmd" run build
   Pop-Location
   ```

5. Restart/refresh the frontend dev server. Keep the already-registered v0.9 backend running.

## What appears

A new **Model intelligence** item appears in the Operations sidebar. It opens a workspace with:
- Status of the three existing SQLite data sources.
- A historical model overview filtered by the dashboard's unified date range.
- A paginated flagged-observation review queue, with vessel search and region filter.
- An observation context panel using the v0.9 feature-deviation endpoint.
- Phase 3 candidate-index statistics.

## Data and interpretation safeguards

- Uses the existing saved model anomaly scores. No retraining is performed.
- The review queue is sorted by the saved anomaly score returned by the API; the score is not displayed as a probability.
- Feature deviations are z-score context, not SHAP values and not a reconstruction of the Isolation Forest decision path.
- Phase 3 entries remain provisional proximity candidates, not confirmed matches.
- Recent AIS/SAR rows remain aggregate grid-cell-hour records without individual vessel identity.

## Rollback

Restore the `App.tsx.backup_<timestamp>` file you created in step 1 and delete `frontend/src/V09AnalyticsWorkspace.tsx`. No backend rollback is needed for this dashboard-only group.

# Inventory platform upgrade — architecture and rollout

This extends the existing Flask app, Mongo products/pricing, PO blueprint and black sidebar. The prior local edits in `tools/import_products_from_access.py` are preserved.

## Confirmed decisions

- Stock_office equals StockA + StockB. The website edits A/B; total is derived. Existing discrepant records are flagged, not silently repaired.
- Login selects an active Mongo username without a password or PIN, as requested. This identifies an account but does not prove the person's identity. Permissions attach to the selected role/account. Deploy behind the company's trusted access boundary.
- Production Access tables are never migrated. The website products and pricing were refreshed only after timestamped Mongo backups. Access write-back requires a path-bound staging receipt and explicit deployment flags.

## Phases

1. Inspect and back up: read-only Mongo metadata confirmed 8 users, 3,028 products and 3,477 pricing records. Three requested names already exist case-insensitively. Timestamped Access file copies are in ignored `backups/`; source size/mtime were stable and copies SHA-256 verified. A copy taken while Access is running is not a substitute for a quiescent production backup.
2. Account selector, stable account IDs, casefold uniqueness, soft deactivation and administrative user history. Migration is a separate explicit command, never an import-time write.
3–4. A/B editing, reviewed before/after values, Mongo transaction updating inventory + append-only movement + durable sync event. Request UUID and revision prevent duplicate/stale changes. Transactions fail closed on unsupported Mongo deployments.
5–6. Windows-only polling worker with parameterised, conditional absolute Access updates, read-back verification, durable local SQLite receipts and an exclusive process lock. No repeated incremental adjustments after acknowledgement failures. Read-only Access snapshots produce observed corrections, never sales. Boot task installer is provided but not executed on the running system.
7–9. History/CSV, real movement analytics, explicitly closed business-day coverage, censored-stockout exclusions, rolling backtests, staged forecasts and transparent replenishment settings. Daily refresh belongs to the worker, not each Gunicorn worker.
10. Seasonal candidates are evaluated only when history is sufficient. No invented historical demand, automatic purchasing, or assumed holiday effects.

## Collections

`users`: stable `_id`, `username`, unique `username_key`, display name, role, active state, timestamps, aliases. `stock_movements`: immutable event ID, snapshots, UTC/Malaysia dates, actor, reason, location deltas, quantities, reference, revision and initial sync status. Current sync status is joined from `sync_events` so the ledger stays append-only.

`sync_events`: durable outbox, before/after expected values, per-product version, status/attempts, acknowledgement and conflict resolution records. `audit_events`: account/configuration/resolution/override history. `inventory_sync_control`: worker heartbeat/checkpoints and manual wake requests; the pre-existing `sync_state` collection is left untouched. `inventory_settings`: permissions and replenishment policy. `business_days`: explicit coverage attestations; unclosed days remain missing, not zero. `daily_demand`: regenerated aggregate features. `forecasts`: versioned results/backtests and input cutoffs. `calendar_events`: Malaysia-local annotations, supplier disruptions. `forecast_overrides`: advisory manual overrides with reasons.

## Mapping

| Access | Mongo / UI |
|---|---|
| Part_id | uid |
| Desc | name |
| Mssid | readable_id |
| Stock_office | stock = stock_a + stock_b |
| Loc_film_box / StockA | location_a / stock_a |
| Loc_wh / StockB | location_b / stock_b |
| Cat | category |
| Supplr | supplier |

The existing `locations` list is mirrored with A/B slot labels for old screens. Cus_Price P/S mappings are unchanged.

## Reliability boundaries

Access has no assumed journal or version column. Conditional snapshot comparison prevents overwriting different observed values, but cannot detect an edit that changes values and later restores them (ABA), nor reconstruct business reasons. External writers must honour Access locking. A local receipt plus target-value comparison handles uncertain commit outcomes without repeating a delta; ambiguous states become conflicts. A Mongo transaction does not make Access and Mongo one distributed transaction.

Sources: [MongoDB transaction/retry semantics](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/transactions/), [Windows scheduled-task startup/account options](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create).

## Deployment sequence

1. Stop Access writes and make a quiescent production backup. `python tools/inspect_upgrade.py --backup` creates an additional timestamped file copy and performs read-only checks; it does not guarantee Access application-level consistency.
2. Install `requirements.txt`; on the Windows bridge install `requirements-sync.txt`. Install `requirements-forecast.txt` only when advanced Holt-Winters evaluation is wanted.
3. Run `python tools/platform_admin.py` for a read-only user migration plan. Review casefold collisions and missing names. Then run `python tools/platform_admin.py --apply` during a maintenance window. This adds only missing requested users and builds indexes; existing accounts are retained.
4. Deploy the web application with `INVENTORY_PLATFORM_ENABLED=1`, `STOCK_WRITES_ENABLED=0`, and `PLATFORM_CONFIG_WRITES=0`. Verify login selection, all read-only pages, existing QR/search/pricing/PO routes, and permissions.
5. Close Access, create a fresh temporary copy, import its A/B baseline into a staging Mongo database, and run `tools/verify_access_copy.py` against a disposable UID. The script performs an A→B transfer and restores the original values. It deliberately produces an incomplete receipt; conflict, uncertain-acknowledgement, offline/reconnect and Windows-restart tests must also pass before the receipt booleans are approved.
6. Put a real `sync-service.env` outside the repository, initially with `SYNC_ENABLED=1`, `SYNC_ENVIRONMENT=staging`, `ACCESS_WRITEBACK_ENABLED=0`. Run `python tools/sync_worker.py --config <absolute path> --once`, inspect `sync-logs/worker.log`, and verify the Inventory Sync page.
7. Enable staging write-back only against the copy. Exercise simultaneous website/Access changes, service termination between Access commit and Mongo acknowledgement, network loss, duplicate request UUIDs, conflict resolution and restart recovery. Confirm no duplicated quantities and restore/compare all staged records.
8. Only after sign-off, create the production staging receipt, set the receipt path and production Access path, then explicitly turn on `STOCK_WRITES_ENABLED=1`, `PLATFORM_CONFIG_WRITES=1`, `SYNC_ENABLED=1`, and `ACCESS_WRITEBACK_ENABLED=1`. The worker refuses production write-back without a complete path-bound receipt.
9. From elevated PowerShell install a machine boot task: `powershell -ExecutionPolicy Bypass -File tools\install_sync_task.ps1 -Action Install -ConfigPath C:\MizitcoSecure\sync-service.env`. If Windows denies machine task registration, install the current-user sign-in launcher with `powershell -ExecutionPolicy Bypass -File tools\install_sync_startup.ps1 -Action Install -ConfigPath <absolute sync-service.env path>`. The sign-in launcher is currently installed on this PC. Use `Start`, `Stop`, `Status`, and `Uninstall` for its lifecycle. The user must sign in after reboot for it to start.

The service uses an OS file lock to prevent overlapping local workers, restarts through Task Scheduler, polls queued web changes every 1–60 seconds, scans Access snapshots on a separately configurable interval, reconnects each cycle, rotates structured JSON logs and stores local idempotency receipts in SQLite. Credentials stay in the protected external environment file.

## Current deployment state and limitations

- The latest Access stock refresh imported five newly changed valid product quantities into Mongo after a timestamped Extended JSON backup. A read-only comparison confirmed zero remaining differences across 1,021 valid Access products. The 1,864 Access rows with missing, negative or inconsistent A/B/total quantities remain in the website but are not eligible for website stock editing or automatic writeback.
- The `Cus_Price` refresh imported 3,738 distinct P/S price records. Thirty exactly identical Access price rows were merged into one record each. Access `RM` is normalized to website `MYR`.
- Native DAO works with this legacy Jet MDB. A copy-based integration test passed website-to-Access transfer, Access-to-website observation, restart idempotency and restoration. The existing 2,839-row `sync_state` collection is preserved; worker control records use `inventory_sync_control`.
- Windows denied a system-level boot task. `tools/install_sync_startup.ps1` installs the worker in the current user's Startup folder, so automatic sync begins when that Windows account signs in. A live worker process and heartbeat must be checked after each rollout.

- The ODBC driver hangs on this MDB; configure `ACCESS_BACKEND=dao` and install `requirements-sync.txt` on the Windows bridge.
- Password-free user selection offers convenience, not identity assurance. A person who can reach the site can select an Admin card. Network access controls and HTTPS/session-secret protection are essential. The user explicitly chose no PIN.
- Access lacks a reliable assumed journal/version field. Snapshot differences become `observed_correction` with unknown business reason. Conditional writes reduce overwrite risk but cannot detect ABA changes or infer missed sales.
- Forecasting starts at Stage 0. No model is trained on current stock. Zero-demand days exist only after an administrator attests that the completed day has full demand/stockout coverage. Stockout/unexplained correction days are excluded, and models need a continuous usable suffix.
- Seasonal holiday effects are annotations only until repeated observations support them. Forecasts and replenishment quantities are advisory and never create POs or stock movements automatically.
- Stage 4 currently evaluates Holt-Winters only when the optional forecasting dependency and advanced flag are enabled, and selects it only when its rolling MAE beats the best simple baseline by at least 2%. SARIMA/SARIMAX and boosting are intentionally deferred until sufficient real history exists to justify and backtest them.

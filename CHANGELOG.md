# Changelog

## 0.2.0

- Add `vehicle_data(vin, endpoint, date_range=None)` for all ten common vehicle endpoints. The client now owns VIN encoding, query construction and the single two-day fallback for refused longer trip queries.
- Return `VehicleData` with the raw payload, response metadata and effective query. Failed API requests expose the effective query in `ApiError.query`.
- Expose `vehicles_metadata` and the learned `trip_max_days` limit without requiring callers to know API URL paths.
- Preserve `get()`, `vehicles()`, authentication renewal, rate-limit backoff, response validation and caller-owned sessions. Existing clients do not need to change.

Validate and publish this client release before shipping an integration that pins `pleos-client==0.2.0`. No Home Assistant data or storage migration is required by the library update.

# Field Manager Team Map

Status: approved implementation contract

Owner: field operations UI

## Page contract

- **Screen identifier and type:** `mobile.manager.team_map`, operational map
  with a synchronized technician list.
- **Audience and permission:** authenticated staff managers holding
  `operations:dispatch:read`. Navigation and the API route use the same gate.
- **Operational job and decision:** locate sharing technicians, distinguish
  current from stale observations, and open dispatch context for the selected
  technician.
- **Authoritative read owner:** `ui.field_live_map_projection`, implemented by
  `app.services.field_maps.list_technician_positions`. The mobile adapter does
  not recalculate sharing visibility or freshness.
- **Primary entities:** sharing-authorized technician positions, keyed by typed
  technician and person identifiers. The separate manager roster provides
  profile and current-work context without precise coordinates.
- **First viewport:** geographic map, live/stale counts, feed freshness, and a
  fit-all control. Search and live/stale/not-sharing filters lead the
  synchronized roster immediately below it.
- **Actions:** tap a marker or roster row to bring the map into view, focus the
  exact shared coordinate, and inspect status, nearest address, observation
  time, accuracy, and current work. The one workflow action opens the dispatch
  queue.
- **Sensitivity:** exact coordinates are private operational data. The map
  owner excludes technicians with sharing disabled before serialization, and
  the roster contract never contains latitude, longitude, accuracy, or the
  location-observation timestamp. The selected-detail owner rechecks sharing
  before sending that one current coordinate to the configured geocoding
  provider; bulk and refresh-time reverse geocoding are forbidden.
- **Freshness:** the owner supplies `is_live` and `stale_after_seconds`; the
  client shows both live and stale positions distinctly and labels last-known
  observation time. It refreshes the feed every 30 seconds only while the app
  and route are active, with pull-to-refresh as an explicit fallback.
- **Ordering:** live technicians first, then stale sharing technicians,
  technicians waiting for a first fix, and non-sharing technicians; names are
  the stable tie-breaker.
- **States:** map and roster independently preserve stable loading, empty,
  partial-refresh failure, initial error with retry, invalid-coordinate, and
  unauthorized presentations. Unknown, stale, off-shift, and not-sharing are
  never collapsed into `Idle`.
- **Responsive and accessibility:** markers use semantic button labels and
  text status in addition to color. Controls remain horizontally scrollable,
  the roster is lazily built, and the map retains a stable mobile height.

## Validation contract

- Backend tests prove disabled sharing and invalid coordinates fail closed.
- Architecture tests prove the mobile adapter calls the typed owner and uses
  `operations:dispatch:read`.
- Flutter widget tests cover live and stale geographic markers, marker detail,
  roster focus, live/last-known address detail, filtering, invalid coordinates,
  and retryable error state.
- The standard mobile viewport must be checked for overflow with one, many,
  and no mapped technicians before release.

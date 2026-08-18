# DotMac Field

Technician/vendor field app for DotMac ISP operations.

This app was moved from `dotmac_crm/mobile` during the CRM-to-sub migration. Its
default API base URL is `https://selfcare.dotmac.io`; local and CI builds can still
override it with `--dart-define=API_BASE_URL=...`.

Field service is work-order execution only. The old CRM field-sales/customer
lookup module was intentionally not carried forward.

On-shift location fixes that cannot be delivered immediately are retained in
the app-private `pending_location_pings.json` queue. The queue holds at most 200
typed fixes, restores on process restart, and is cleared only after the field
location API accepts the batch. It is independent of the business-mutation
outbox so location transport failures cannot block job transitions or requests.
Malformed queue data is deleted rather than retried or retained with private
coordinates; a payload-free `.corrupt` marker records when recovery occurred.
After upload, the server retains detailed GPS ping history for 30 days based on
its receipt timestamp; current presence and work-order evidence have separate
lifecycles.

Vendor mode uses the same sub-native work-order execution tabs as technicians,
with backend scoping by vendor assignment. Do not re-add CRM project/quote
routes; vendor work must come back as sub-native work orders.

## Useful Commands

```sh
flutter pub get
dart run build_runner build --delete-conflicting-outputs
flutter test
```

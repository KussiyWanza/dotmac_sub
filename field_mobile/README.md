# DotMac Field

Technician/vendor field app for DotMac ISP operations.

This app was moved from `dotmac_crm/mobile` during the CRM-to-sub migration. Its
default API base URL is `https://selfcare.dotmac.io`; local and CI builds can still
override it with `--dart-define=API_BASE_URL=...`.

Field service is work-order execution only. The old CRM field-sales/customer
lookup module was intentionally not carried forward.

Vendor mode uses the same sub-native work-order execution tabs as technicians,
with backend scoping by vendor assignment. Do not re-add CRM project/quote
routes; vendor work must come back as sub-native work orders.

## Useful Commands

```sh
flutter pub get
dart run build_runner build --delete-conflicting-outputs
flutter test
```

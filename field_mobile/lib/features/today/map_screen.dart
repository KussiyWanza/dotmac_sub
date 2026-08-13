import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:latlong2/latlong.dart';

import '../../app/theme.dart';
import '../../core/location/map_coordinates.dart';
import '../execution/execution_controller.dart';
import '../jobs/job_models.dart';
import '../jobs/jobs_providers.dart';
import '../jobs/location_pin_screen.dart';
import 'asset_pin_screen.dart';
import 'map_assets_repository.dart';
import 'map_models.dart';

final mapPinsProvider = FutureProvider<List<JobPin>>((ref) async {
  final jobs = (await ref.watch(jobsListProvider.future)).jobs;
  final db = ref.watch(syncServiceProvider).db;
  final cached = await db.select(db.cachedJobs).get();
  final detailById = {for (final row in cached) row.id: row.detailJson};
  return buildJobPins(jobs, detailById);
});

class MapScreen extends ConsumerStatefulWidget {
  const MapScreen({super.key, this.showTiles = true, this.focusJobId});

  /// Disabled in widget tests so no tile HTTP requests are made.
  final bool showTiles;

  /// When opened from a job card, center the map on that job's pin.
  final String? focusJobId;

  @override
  ConsumerState<MapScreen> createState() => _MapScreenState();
}

class _MapScreenState extends ConsumerState<MapScreen> {
  final _mapController = MapController();
  String _searchQuery = '';

  @override
  void dispose() {
    _mapController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final pins = ref.watch(mapPinsProvider);
    final assets = ref.watch(mapAssetsProvider);
    final selectedTypes = ref.watch(selectedMapAssetTypesProvider);
    final onlineSearch = ref.watch(mapPlaceSearchProvider(_searchQuery));

    return Scaffold(
      appBar: AppBar(
        title: const Text('Job map'),
        actions: [
          pins.maybeWhen(
            data: (items) => TextButton.icon(
              key: const Key('edit-pins-button'),
              onPressed: () => _showPinListSheet(
                context,
                ref,
                items.where((pin) => pin.hasValidCoordinates).toList(),
                (assets.valueOrNull ?? const <MapAsset>[])
                    .where((asset) => asset.hasValidCoordinates)
                    .toList(),
              ),
              icon: const Icon(Icons.push_pin_outlined),
              label: const Text('Edit'),
            ),
            orElse: () => TextButton.icon(
              key: const Key('edit-pins-button'),
              onPressed: null,
              icon: const Icon(Icons.push_pin_outlined),
              label: const Text('Edit'),
            ),
          ),
        ],
      ),
      body: pins.when(
        data: (items) {
          final validPins = items
              .where((pin) => pin.hasValidCoordinates)
              .toList();
          final focusedPin = widget.focusJobId == null
              ? null
              : validPins.cast<JobPin?>().firstWhere(
                  (pin) => pin?.id == widget.focusJobId,
                  orElse: () => null,
                );
          final assetItems =
              (assets.valueOrNull ?? const <MapAsset>[])
                  .where((asset) => asset.hasValidCoordinates)
                  .toList()
                ..sort(
                  (a, b) => _assetPaintRank(
                    a.type,
                  ).compareTo(_assetPaintRank(b.type)),
                );
          final center = focusedPin != null
              ? safeLatLng(focusedPin.latitude, focusedPin.longitude)!
              : validPins.isNotEmpty
              ? safeLatLng(validPins.first.latitude, validPins.first.longitude)!
              : assetItems.isNotEmpty
              ? safeLatLng(
                  assetItems.first.latitude,
                  assetItems.first.longitude,
                )!
              : defaultMapCenter;
          return Stack(
            children: [
              FlutterMap(
                mapController: _mapController,
                options: MapOptions(
                  initialCenter: center,
                  initialZoom: focusedPin == null ? 12 : 16,
                  cameraConstraint: finiteMapCameraConstraint,
                ),
                children: [
                  if (widget.showTiles)
                    TileLayer(
                      urlTemplate:
                          'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                      userAgentPackageName: 'io.dotmac.dotmac_field',
                    ),
                  MarkerLayer(
                    markers: [
                      for (final asset in assetItems)
                        Marker(
                          point: safeLatLng(asset.latitude, asset.longitude)!,
                          width: 38,
                          height: 38,
                          child: GestureDetector(
                            key: Key('asset-${asset.type}-${asset.id}'),
                            onTap: () => _showAssetSheet(context, ref, asset),
                            child: Icon(
                              _assetIcon(asset.type),
                              size: 30,
                              color: _assetColor(asset.type),
                            ),
                          ),
                        ),
                      for (final pin in validPins)
                        Marker(
                          point: safeLatLng(pin.latitude, pin.longitude)!,
                          width: 44,
                          height: 44,
                          child: GestureDetector(
                            key: Key('pin-${pin.id}'),
                            onTap: () => _showJobSheet(context, ref, pin),
                            child: Icon(
                              Icons.location_pin,
                              size: 40,
                              color: AppColors.statusTone(
                                context,
                                pin.presentation.tone,
                              ),
                            ),
                          ),
                        ),
                    ],
                  ),
                  if (widget.showTiles)
                    const Align(
                      alignment: Alignment.bottomLeft,
                      child: Padding(
                        padding: EdgeInsets.all(4),
                        child: Text(
                          '© OpenStreetMap contributors',
                          style: TextStyle(fontSize: 10),
                        ),
                      ),
                    ),
                ],
              ),
              _MapSearchOverlay(
                query: _searchQuery,
                results: _searchResults(
                  validPins,
                  assetItems,
                  onlineSearch.valueOrNull ?? const [],
                ),
                loadingOnline:
                    _searchQuery.trim().length >= 2 && onlineSearch.isLoading,
                onQueryChanged: (value) => setState(() => _searchQuery = value),
                onSelected: _selectSearchResult,
              ),
              _LayerSelector(
                selectedTypes: selectedTypes,
                loadingAssets: assets.isLoading,
                top: _searchQuery.trim().isEmpty ? 76 : 180,
                onChanged: (type, selected) {
                  final next = {...selectedTypes};
                  if (selected) {
                    next.add(type);
                  } else {
                    next.remove(type);
                  }
                  ref.read(selectedMapAssetTypesProvider.notifier).state = next;
                },
              ),
            ],
          );
        },
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) => const Center(child: Text('Could not load the map')),
      ),
    );
  }

  List<_MapSearchResult> _searchResults(
    List<JobPin> pins,
    List<MapAsset> assets,
    List<MapPlaceSearchResult> onlineResults,
  ) {
    final query = _searchQuery.trim().toLowerCase();
    if (query.isEmpty) return const [];
    final results = <_MapSearchResult>[];
    final seen = <String>{};
    for (final pin in pins) {
      if (_matches(query, [pin.title, pin.status, pin.addressText])) {
        _appendSearchResult(results, seen, _MapSearchResult.job(pin));
      }
    }
    for (final asset in assets) {
      if (_matches(query, [
        asset.title,
        asset.subtitle,
        asset.status,
        asset.type,
        mapAssetTypeLabels[asset.type],
      ])) {
        _appendSearchResult(results, seen, _MapSearchResult.asset(asset));
      }
    }
    for (final result in onlineResults) {
      if (result.kind == 'job') {
        _appendSearchResult(
          results,
          seen,
          _MapSearchResult.job(
            JobPin(
              id: result.id,
              title: result.title,
              status: result.status ?? 'scheduled',
              statusPresentation: result.statusPresentation,
              latitude: result.latitude,
              longitude: result.longitude,
              addressText: result.addressText ?? result.subtitle,
            ),
          ),
        );
      } else if (result.kind == 'asset' && result.assetType != null) {
        _appendSearchResult(
          results,
          seen,
          _MapSearchResult.asset(
            MapAsset(
              id: result.id,
              type: result.assetType!,
              title: result.title,
              subtitle: result.subtitle,
              latitude: result.latitude,
              longitude: result.longitude,
              status: result.status,
            ),
          ),
        );
      }
    }
    return results.take(6).toList();
  }

  void _appendSearchResult(
    List<_MapSearchResult> results,
    Set<String> seen,
    _MapSearchResult result,
  ) {
    if (seen.add(result.id)) results.add(result);
  }

  bool _matches(String query, Iterable<String?> values) {
    return values.any(
      (value) => value != null && value.toLowerCase().contains(query),
    );
  }

  void _selectSearchResult(_MapSearchResult result) {
    final point = result.point;
    _mapController.move(point, 16);
    FocusScope.of(context).unfocus();
    setState(() => _searchQuery = result.title);
    switch (result) {
      case _JobSearchResult(:final pin):
        _showJobSheet(context, ref, pin);
      case _AssetSearchResult(:final asset):
        _showAssetSheet(context, ref, asset);
    }
  }

  Future<void> _showJobSheet(
    BuildContext context,
    WidgetRef ref,
    JobPin pin,
  ) async {
    final action = await showModalBottomSheet<_MapSheetAction>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: Icon(
                Icons.assignment_outlined,
                color: AppColors.statusTone(context, pin.presentation.tone),
              ),
              title: Text(pin.title),
              subtitle: Text(pin.presentation.label),
              trailing: const Icon(Icons.chevron_right),
              onTap: () {
                Navigator.pop(sheetContext, _MapSheetAction.open);
              },
            ),
            ListTile(
              leading: const Icon(Icons.push_pin_outlined),
              title: const Text('Edit pin location'),
              onTap: () => Navigator.pop(sheetContext, _MapSheetAction.edit),
            ),
          ],
        ),
      ),
    );
    if (!context.mounted) return;
    switch (action) {
      case _MapSheetAction.open:
        context.push('/jobs/${pin.id}');
      case _MapSheetAction.edit:
        final changed = await Navigator.of(context).push<bool>(
          MaterialPageRoute(
            builder: (_) => LocationPinScreen(
              jobId: pin.id,
              initialLocation: JobLocation(
                latitude: pin.latitude,
                longitude: pin.longitude,
                source: 'cached',
              ),
            ),
          ),
        );
        if (context.mounted && changed == true) ref.invalidate(mapPinsProvider);
      case null:
        break;
    }
  }

  Future<void> _showAssetSheet(
    BuildContext context,
    WidgetRef ref,
    MapAsset asset,
  ) async {
    final label = mapAssetTypeLabels[asset.type] ?? asset.type;
    final action = await showModalBottomSheet<_MapSheetAction>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: Icon(
                _assetIcon(asset.type),
                color: _assetColor(asset.type),
              ),
              title: Text(asset.title),
              subtitle: Text(
                [
                  label,
                  if (asset.subtitle != null) asset.subtitle!,
                  if (asset.status != null) asset.status!,
                ].join(' · '),
              ),
            ),
            ListTile(
              leading: const Icon(Icons.push_pin_outlined),
              title: const Text('Edit asset location'),
              onTap: () => Navigator.pop(sheetContext, _MapSheetAction.edit),
            ),
          ],
        ),
      ),
    );
    if (!context.mounted || action != _MapSheetAction.edit) return;
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(builder: (_) => AssetPinScreen(asset: asset)),
    );
    if (context.mounted && changed == true) ref.invalidate(mapAssetsProvider);
  }

  Future<void> _showPinListSheet(
    BuildContext context,
    WidgetRef ref,
    List<JobPin> pins,
    List<MapAsset> assets,
  ) async {
    final selection = await showModalBottomSheet<_MapEditSelection>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
              child: Text(
                'Edit map pin',
                style: Theme.of(context).textTheme.titleMedium,
              ),
            ),
            if (pins.isEmpty && assets.isEmpty)
              const ListTile(
                leading: Icon(Icons.info_outline),
                title: Text('No pins loaded yet'),
              ),
            for (final pin in pins)
              ListTile(
                leading: Icon(
                  Icons.location_pin,
                  color: AppColors.statusTone(context, pin.presentation.tone),
                ),
                title: Text(pin.title),
                subtitle: Text(pin.presentation.label),
                onTap: () =>
                    Navigator.pop(sheetContext, _JobEditSelection(pin)),
              ),
            for (final asset in assets)
              ListTile(
                leading: Icon(
                  _assetIcon(asset.type),
                  color: _assetColor(asset.type),
                ),
                title: Text(asset.title),
                subtitle: Text(mapAssetTypeLabels[asset.type] ?? asset.type),
                onTap: () =>
                    Navigator.pop(sheetContext, _AssetEditSelection(asset)),
              ),
          ],
        ),
      ),
    );
    if (!context.mounted || selection == null) return;
    switch (selection) {
      case _JobEditSelection(:final pin):
        final changed = await Navigator.of(context).push<bool>(
          MaterialPageRoute(
            builder: (_) => LocationPinScreen(
              jobId: pin.id,
              initialLocation: JobLocation(
                latitude: pin.latitude,
                longitude: pin.longitude,
                source: 'cached',
              ),
            ),
          ),
        );
        if (context.mounted && changed == true) ref.invalidate(mapPinsProvider);
      case _AssetEditSelection(:final asset):
        final changed = await Navigator.of(context).push<bool>(
          MaterialPageRoute(builder: (_) => AssetPinScreen(asset: asset)),
        );
        if (context.mounted && changed == true) {
          ref.invalidate(mapAssetsProvider);
        }
    }
  }
}

sealed class _MapSearchResult {
  const _MapSearchResult();

  factory _MapSearchResult.job(JobPin pin) = _JobSearchResult;
  factory _MapSearchResult.asset(MapAsset asset) = _AssetSearchResult;

  String get id;
  String get title;
  String get subtitle;
  IconData get icon;
  Color color(BuildContext context);
  LatLng get point;
}

enum _MapSheetAction { open, edit }

sealed class _MapEditSelection {
  const _MapEditSelection();
}

class _JobEditSelection extends _MapEditSelection {
  const _JobEditSelection(this.pin);

  final JobPin pin;
}

class _AssetEditSelection extends _MapEditSelection {
  const _AssetEditSelection(this.asset);

  final MapAsset asset;
}

class _JobSearchResult extends _MapSearchResult {
  const _JobSearchResult(this.pin);

  final JobPin pin;

  @override
  String get id => 'job-${pin.id}';

  @override
  String get title => pin.title;

  @override
  String get subtitle => [
    if (pin.addressText != null) pin.addressText!,
    pin.presentation.label,
  ].join(' · ');

  @override
  IconData get icon => Icons.location_pin;

  @override
  Color color(BuildContext context) =>
      AppColors.statusTone(context, pin.presentation.tone);

  @override
  LatLng get point => safeLatLng(pin.latitude, pin.longitude)!;
}

class _AssetSearchResult extends _MapSearchResult {
  const _AssetSearchResult(this.asset);

  final MapAsset asset;

  @override
  String get id => 'asset-${asset.type}-${asset.id}';

  @override
  String get title => asset.title;

  @override
  String get subtitle => [
    mapAssetTypeLabels[asset.type] ?? asset.type,
    if (asset.subtitle != null) asset.subtitle!,
    if (asset.status != null) asset.status!,
  ].join(' · ');

  @override
  IconData get icon => _assetIcon(asset.type);

  @override
  Color color(BuildContext context) => _assetColor(asset.type);

  @override
  LatLng get point => safeLatLng(asset.latitude, asset.longitude)!;
}

class _MapSearchOverlay extends StatelessWidget {
  const _MapSearchOverlay({
    required this.query,
    required this.results,
    required this.loadingOnline,
    required this.onQueryChanged,
    required this.onSelected,
  });

  final String query;
  final List<_MapSearchResult> results;
  final bool loadingOnline;
  final ValueChanged<String> onQueryChanged;
  final ValueChanged<_MapSearchResult> onSelected;

  @override
  Widget build(BuildContext context) {
    final hasQuery = query.trim().isNotEmpty;
    return Positioned(
      top: 12,
      left: 12,
      right: 12,
      child: Material(
        color: Theme.of(context).colorScheme.surface,
        elevation: 3,
        borderRadius: BorderRadius.circular(8),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              key: const Key('map-search-field'),
              onChanged: onQueryChanged,
              textInputAction: TextInputAction.search,
              decoration: InputDecoration(
                hintText: 'Search places',
                prefixIcon: const Icon(Icons.search),
                suffixIcon: hasQuery
                    ? IconButton(
                        tooltip: 'Clear search',
                        onPressed: () => onQueryChanged(''),
                        icon: const Icon(Icons.close),
                      )
                    : null,
                border: InputBorder.none,
                contentPadding: const EdgeInsets.symmetric(vertical: 14),
              ),
            ),
            if (hasQuery) const Divider(height: 1),
            if (hasQuery)
              ConstrainedBox(
                constraints: const BoxConstraints(maxHeight: 148),
                child: loadingOnline && results.isEmpty
                    ? const ListTile(
                        dense: true,
                        leading: SizedBox.square(
                          dimension: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        ),
                        title: Text('Searching places'),
                      )
                    : results.isEmpty
                    ? const ListTile(
                        dense: true,
                        leading: Icon(Icons.search_off_outlined),
                        title: Text('No matching places'),
                      )
                    : ListView.builder(
                        shrinkWrap: true,
                        padding: EdgeInsets.zero,
                        itemCount: results.length,
                        itemBuilder: (context, index) {
                          final result = results[index];
                          return ListTile(
                            key: Key('map-search-result-${result.id}'),
                            dense: true,
                            leading: Icon(
                              result.icon,
                              color: result.color(context),
                            ),
                            title: Text(result.title),
                            subtitle: Text(result.subtitle),
                            onTap: () => onSelected(result),
                          );
                        },
                      ),
              ),
          ],
        ),
      ),
    );
  }
}

class _LayerSelector extends StatelessWidget {
  const _LayerSelector({
    required this.selectedTypes,
    required this.loadingAssets,
    required this.top,
    required this.onChanged,
  });

  final Set<String> selectedTypes;
  final bool loadingAssets;
  final double top;
  final void Function(String type, bool selected) onChanged;

  @override
  Widget build(BuildContext context) {
    return Positioned(
      top: top,
      left: 12,
      right: 12,
      child: Material(
        color: Theme.of(context).colorScheme.surface,
        elevation: 2,
        borderRadius: BorderRadius.circular(8),
        child: SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
          child: Row(
            children: [
              if (loadingAssets)
                const Padding(
                  padding: EdgeInsets.only(right: 8),
                  child: SizedBox.square(
                    dimension: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                ),
              for (final entry in mapAssetTypeLabels.entries)
                Padding(
                  padding: const EdgeInsets.only(right: 6),
                  child: FilterChip(
                    label: Text(entry.value),
                    selected: selectedTypes.contains(entry.key),
                    onSelected: (selected) => onChanged(entry.key, selected),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

IconData _assetIcon(String type) => switch (type) {
  'olt' => Icons.router_outlined,
  'fdh' => Icons.hub_outlined,
  'fiber_access_point' => Icons.device_hub_outlined,
  'splice_closure' => Icons.join_inner_outlined,
  'wireless_mast' => Icons.cell_tower_outlined,
  'service_building' => Icons.apartment_outlined,
  _ => Icons.place_outlined,
};

Color _assetColor(String type) => switch (type) {
  'olt' => AppColors.category(0),
  'fdh' => AppColors.category(1),
  'fiber_access_point' => AppColors.category(2),
  'splice_closure' => AppColors.category(4),
  'wireless_mast' => AppColors.category(5),
  'service_building' => AppColors.category(6),
  _ => AppColors.category(6),
};

int _assetPaintRank(String type) => switch (type) {
  'service_building' => 0,
  'fiber_access_point' => 1,
  'wireless_mast' => 2,
  'splice_closure' => 3,
  'fdh' => 4,
  'olt' => 5,
  _ => 0,
};

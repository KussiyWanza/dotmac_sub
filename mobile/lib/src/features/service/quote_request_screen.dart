import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:geolocator/geolocator.dart';
import 'package:go_router/go_router.dart';
import 'package:latlong2/latlong.dart';

import '../../core/api_exception.dart';
import '../../providers/data_providers.dart';

/// Drop a map pin for a new installation; the active quote owner returns
/// feasibility, an estimate, and the required deposit.
class QuoteRequestScreen extends ConsumerStatefulWidget {
  const QuoteRequestScreen({super.key});

  @override
  ConsumerState<QuoteRequestScreen> createState() => _QuoteRequestScreenState();
}

class _QuoteRequestScreenState extends ConsumerState<QuoteRequestScreen> {
  static const _fallbackCenter = LatLng(9.0563, 7.4985); // Abuja

  final _mapController = MapController();
  final _mapKey = GlobalKey();
  final _address = TextEditingController();
  final _note = TextEditingController();

  LatLng? _selected;
  Timer? _reverseDebounce;
  int _selectionRevision = 0;
  bool _addressEdited = false;
  bool _locating = false;
  bool _submitting = false;

  @override
  void dispose() {
    _reverseDebounce?.cancel();
    _address.dispose();
    _note.dispose();
    super.dispose();
  }

  void _snack(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  void _select(LatLng point, {bool recenter = false}) {
    setState(() {
      _selected = point;
      _selectionRevision++;
      _addressEdited = false;
      _address.clear();
    });
    if (recenter) _mapController.move(point, 17);
    _reverseGeocode(point);
  }

  LatLng? _globalToLatLng(Offset global) {
    final box = _mapKey.currentContext?.findRenderObject() as RenderBox?;
    if (box == null) return null;
    return _mapController.camera.screenOffsetToLatLng(
      box.globalToLocal(global),
    );
  }

  void _reverseGeocode(LatLng point) {
    _reverseDebounce?.cancel();
    final revision = _selectionRevision;
    _reverseDebounce = Timer(const Duration(milliseconds: 400), () async {
      try {
        final address = await ref
            .read(locationRepositoryProvider)
            .reverseGeocode(point.latitude, point.longitude);
        if (mounted &&
            revision == _selectionRevision &&
            !_addressEdited &&
            address != null) {
          _address.text = address;
        }
      } on ApiException {
        // best-effort; the pin is what gets submitted
      }
    });
  }

  void _beginPinMove() {
    setState(() {
      _selectionRevision++;
      _addressEdited = false;
      _address.clear();
    });
  }

  Future<void> _useMyLocation() async {
    setState(() => _locating = true);
    try {
      if (!await Geolocator.isLocationServiceEnabled()) {
        throw const LocationServiceDisabledException();
      }
      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }
      if (permission == LocationPermission.denied ||
          permission == LocationPermission.deniedForever) {
        throw const PermissionDeniedException('denied');
      }
      final position = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 15),
        ),
      );
      if (!mounted) return;
      _select(LatLng(position.latitude, position.longitude), recenter: true);
    } on LocationServiceDisabledException {
      _snack('Turn on location services to use your GPS position.');
    } catch (_) {
      _snack('Location permission is needed to use your GPS position.');
    } finally {
      if (mounted) setState(() => _locating = false);
    }
  }

  Future<void> _submit() async {
    if (_submitting) return;
    FocusScope.of(context).unfocus();
    final selected = _selected;
    if (selected == null) {
      _snack('Drop a pin on your installation address first.');
      return;
    }
    setState(() => _submitting = true);
    try {
      await ref.read(quotesRepositoryProvider).requestQuote(
            latitude: selected.latitude,
            longitude: selected.longitude,
            address: _address.text.trim(),
            note: _note.text.trim(),
          );
      ref.invalidate(quotesProvider);
      if (!mounted) return;
      _snack('Estimate ready — see your quote.');
      context.pop();
    } on ApiException catch (e) {
      _snack(e.message);
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final quotesPage = ref.watch(quotesProvider);
    return quotesPage.when(
      loading: () => Scaffold(
        appBar: AppBar(title: const Text('Request installation')),
        body: const Center(child: CircularProgressIndicator()),
      ),
      error: (_, __) => Scaffold(
        appBar: AppBar(title: const Text('Request installation')),
        body: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  'Could not confirm whether online quotes are available.',
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 16),
                FilledButton.tonal(
                  onPressed: () => ref.invalidate(quotesProvider),
                  child: const Text('Try again'),
                ),
              ],
            ),
          ),
        ),
      ),
      data: (page) => page.actionsAvailable
          ? _buildRequestForm(context)
          : _unavailable(context, page.actionsUnavailableMessage),
    );
  }

  Widget _unavailable(BuildContext context, String? ownerMessage) {
    return Scaffold(
      appBar: AppBar(title: const Text('Request installation')),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                Icons.info_outline,
                size: 48,
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
              const SizedBox(height: 16),
              Text(
                ownerMessage ??
                    'Online quote requests are currently unavailable. '
                        'Please contact support to continue.',
                textAlign: TextAlign.center,
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildRequestForm(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final center = _selected ?? _fallbackCenter;
    return Scaffold(
      appBar: AppBar(title: const Text('Request installation')),
      body: Column(
        children: [
          Expanded(
            child: Stack(
              children: [
                FlutterMap(
                  key: _mapKey,
                  mapController: _mapController,
                  options: MapOptions(
                    initialCenter: center,
                    initialZoom: 16,
                    onTap: (_, point) => _select(point),
                  ),
                  children: [
                    TileLayer(
                      urlTemplate:
                          'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                      userAgentPackageName: 'io.dotmac.selfcare',
                    ),
                    if (_selected != null)
                      MarkerLayer(
                        markers: [
                          Marker(
                            point: _selected!,
                            width: 48,
                            height: 48,
                            alignment: Alignment.topCenter,
                            child: GestureDetector(
                              onPanStart: (_) => _beginPinMove(),
                              onPanUpdate: (d) {
                                final p = _globalToLatLng(d.globalPosition);
                                if (p != null) setState(() => _selected = p);
                              },
                              onPanEnd: (_) {
                                final p = _selected;
                                if (p != null) _reverseGeocode(p);
                              },
                              child: Icon(
                                Icons.place,
                                key: const ValueKey('installation-map-pin'),
                                color: scheme.primary,
                                size: 44,
                              ),
                            ),
                          ),
                        ],
                      ),
                  ],
                ),
                Positioned(
                  right: 12,
                  bottom: 12,
                  child: FloatingActionButton.small(
                    heroTag: 'gps',
                    onPressed: _locating ? null : _useMyLocation,
                    child: _locating
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.my_location),
                  ),
                ),
              ],
            ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    _selected == null
                        ? 'Tap the map (or use GPS) to pin your installation address.'
                        : 'Pinned location selected. Confirm the address below.',
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    key: const ValueKey('installation-address'),
                    controller: _address,
                    textInputAction: TextInputAction.next,
                    onChanged: (_) => _addressEdited = true,
                    decoration: const InputDecoration(
                      labelText: 'Installation address',
                      hintText: 'Enter or confirm the pinned address',
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    key: const ValueKey('installation-notes'),
                    controller: _note,
                    maxLines: 2,
                    textInputAction: TextInputAction.done,
                    onSubmitted: (_) {
                      if (_selected != null && !_submitting) _submit();
                    },
                    decoration: const InputDecoration(
                      labelText: 'Notes (building, floor, landmark…)',
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 12),
                  FilledButton(
                    key: const ValueKey('get-estimate'),
                    onPressed:
                        _submitting || _selected == null ? null : _submit,
                    child: _submitting
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Text('Get my estimate'),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

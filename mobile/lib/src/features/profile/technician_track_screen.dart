import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:latlong2/latlong.dart';

import '../../models/technician_location.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Live "where's my technician" map for an in-progress work order. Polls the
/// technician's position (~20s) and hides itself outside the active visit
/// window (the CRM returns available=false with a reason).
class TechnicianTrackScreen extends ConsumerWidget {
  const TechnicianTrackScreen({super.key, required this.workOrderId});

  final String workOrderId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final loc = ref.watch(technicianLocationProvider(workOrderId));
    return Scaffold(
      appBar: AppBar(title: const Text('Track technician')),
      body: AsyncValueView<TechnicianLocation>(
        value: loc,
        onRetry: () => ref.invalidate(technicianLocationProvider(workOrderId)),
        data: (location) {
          if (!location.available ||
              location.latitude == null ||
              location.longitude == null) {
            return _unavailable(context, location.reason);
          }
          final point = LatLng(location.latitude!, location.longitude!);
          return Column(
            children: [
              if (location.estimatedArrivalAt != null)
                _etaBanner(context, location.estimatedArrivalAt!),
              Expanded(
                child: FlutterMap(
                  options: MapOptions(initialCenter: point, initialZoom: 15),
                  children: [
                    TileLayer(
                      urlTemplate:
                          'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                      userAgentPackageName: 'io.dotmac.selfcare',
                    ),
                    MarkerLayer(
                      markers: [
                        Marker(
                          point: point,
                          width: 46,
                          height: 46,
                          child: Icon(
                            Icons.engineering,
                            size: 40,
                            color: Theme.of(context).colorScheme.primary,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  Widget _etaBanner(BuildContext context, DateTime eta) {
    final t = '${eta.hour.toString().padLeft(2, '0')}:'
        '${eta.minute.toString().padLeft(2, '0')}';
    return Container(
      width: double.infinity,
      color: Theme.of(context).colorScheme.primaryContainer,
      padding: const EdgeInsets.all(12),
      child: Text(
        'Estimated arrival around $t',
        textAlign: TextAlign.center,
        style: TextStyle(
          color: Theme.of(context).colorScheme.onPrimaryContainer,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }

  Widget _unavailable(BuildContext context, String? reason) {
    final message = switch (reason) {
      'not_in_progress' =>
        'Live tracking starts once the technician begins your visit.',
      'sharing_off' =>
        "The technician's live location isn't being shared right now.",
      'no_fix' => "Waiting for the technician's location…",
      'stale' => "Waiting for an updated technician location...",
      'not_linked' => "Live tracking isn't available for this visit.",
      _ => "Live tracking isn't available right now.",
    };
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.location_off_outlined,
              size: 48,
              color: Theme.of(context).colorScheme.outline,
            ),
            const SizedBox(height: 16),
            Text(message, textAlign: TextAlign.center),
          ],
        ),
      ),
    );
  }
}

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../auth/auth_state.dart';
import 'material_models.dart';

class MaterialsRepository {
  const MaterialsRepository(this._ref);

  final Ref _ref;

  Future<List<InventoryItem>> searchInventory(
    String query, {
    String? sourceLocationId,
  }) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get(
          '/api/v1/field/inventory/items',
          queryParameters: {
            if (query.trim().isNotEmpty) 'q': query.trim(),
            if (sourceLocationId != null && sourceLocationId.trim().isNotEmpty)
              'source_location_id': sourceLocationId.trim(),
            'limit': 30,
          },
        );
    return _items(response.data).map(InventoryItem.fromJson).toList();
  }

  Future<List<InventoryLocation>> fetchLocations() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get(
          '/api/v1/field/inventory/locations',
          queryParameters: {'limit': 100},
        );
    return _items(response.data).map(InventoryLocation.fromJson).toList();
  }

  Future<List<MaterialRequest>> fetchRequests() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get(
          '/api/v1/field/material-requests',
          queryParameters: {'limit': 100},
        );
    return _items(response.data).map(MaterialRequest.fromJson).toList();
  }

  Future<MaterialRequest> fetchRequest(String id) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/material-requests/$id');
    return MaterialRequest.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<MaterialRequest> createRequest({
    required String priority,
    required List<MaterialRequestItemDraft> items,
    String? clientRef,
    String? notes,
    String? workOrderId,
    String? projectId,
    String? ticketId,
    String? sourceLocationId,
    String? sourceWarehouseCode,
    String? destinationLocationId,
    bool submit = true,
  }) async {
    if (submit && (clientRef == null || clientRef.trim().isEmpty)) {
      throw ArgumentError(
        'clientRef is required when submitting a material request',
      );
    }
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post(
          submit
              ? '/api/v1/field/material-requests/submit'
              : '/api/v1/field/material-requests',
          data: buildMaterialRequestPayload(
            priority: priority,
            clientRef: clientRef,
            notes: notes,
            workOrderId: workOrderId,
            projectId: projectId,
            ticketId: ticketId,
            sourceLocationId: sourceLocationId,
            sourceWarehouseCode: sourceWarehouseCode,
            destinationLocationId: destinationLocationId,
            items: items,
            submit: submit,
          ),
        );
    return MaterialRequest.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<MaterialRequest> submitRequest(String id) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post('/api/v1/field/material-requests/$id/submit');
    return MaterialRequest.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }
}

Map<String, dynamic> buildMaterialRequestPayload({
  required String priority,
  required List<MaterialRequestItemDraft> items,
  String? clientRef,
  String? notes,
  String? workOrderId,
  String? projectId,
  String? ticketId,
  String? sourceLocationId,
  String? sourceWarehouseCode,
  String? destinationLocationId,
  bool submit = true,
}) => {
  'priority': priority,
  if (clientRef != null && clientRef.trim().isNotEmpty)
    'client_ref': clientRef.trim(),
  if (notes != null && notes.trim().isNotEmpty) 'notes': notes.trim(),
  if (workOrderId != null && workOrderId.trim().isNotEmpty)
    'work_order_id': workOrderId.trim(),
  if (projectId != null && projectId.trim().isNotEmpty)
    'project_id': projectId.trim(),
  if (ticketId != null && ticketId.trim().isNotEmpty)
    'ticket_id': ticketId.trim(),
  if (sourceWarehouseCode != null && sourceWarehouseCode.trim().isNotEmpty)
    'source_warehouse_code': sourceWarehouseCode.trim(),
  'items': items.map((item) => item.toJson()).toList(),
};

List<Map<String, dynamic>> _items(Object? data) {
  if (data is Map && data['items'] is List) {
    return _mapItems(data['items']);
  }
  if (data is Map) {
    for (final key in ['data', 'results', 'material_requests', 'requests']) {
      final nested = data[key];
      if (nested is List) {
        return _mapItems(nested);
      }
      if (nested is Map) {
        final nestedItems = _items(nested);
        if (nestedItems.isNotEmpty) return nestedItems;
      }
    }
  }
  if (data is List) {
    return _mapItems(data);
  }
  return const [];
}

List<Map<String, dynamic>> _mapItems(Object? raw) {
  if (raw is! List) return const [];
  return [
    for (final item in raw)
      if (item is Map) item.cast<String, dynamic>(),
  ];
}

final materialsRepositoryProvider = Provider<MaterialsRepository>(
  MaterialsRepository.new,
);

final materialRequestsProvider = FutureProvider<List<MaterialRequest>>(
  (ref) => ref.watch(materialsRepositoryProvider).fetchRequests(),
);

final materialRequestProvider = FutureProvider.family<MaterialRequest, String>(
  (ref, id) => ref.watch(materialsRepositoryProvider).fetchRequest(id),
);

final inventorySearchQueryProvider = StateProvider.autoDispose<String>(
  (ref) => '',
);

final inventorySourceLocationProvider = StateProvider.autoDispose<String?>(
  (ref) => null,
);

final inventorySearchProvider = FutureProvider.autoDispose<List<InventoryItem>>(
  (ref) {
    final query = ref.watch(inventorySearchQueryProvider);
    final sourceLocationId = ref.watch(inventorySourceLocationProvider);
    return ref
        .watch(materialsRepositoryProvider)
        .searchInventory(query, sourceLocationId: sourceLocationId);
  },
);

final inventoryLocationsProvider = FutureProvider<List<InventoryLocation>>(
  (ref) => ref.watch(materialsRepositoryProvider).fetchLocations(),
);

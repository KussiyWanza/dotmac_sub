class InventoryItem {
  const InventoryItem({
    required this.id,
    required this.name,
    this.sku,
    this.unit,
    this.unitPrice,
    this.currency,
    this.availableQuantity,
    this.stockByLocation = const [],
  });

  final String id;
  final String name;
  final String? sku;
  final String? unit;
  final double? unitPrice;
  final String? currency;
  final int? availableQuantity;
  final List<InventoryLocationStock> stockByLocation;

  String get displayName => sku == null || sku!.isEmpty ? name : '$name ($sku)';

  factory InventoryItem.fromJson(Map<String, dynamic> json) => InventoryItem(
    id: json['id'].toString(),
    name: _string(json['name']) ?? 'Item',
    sku: _string(json['sku']),
    unit: _string(json['unit']),
    unitPrice: _double(json['unit_price']),
    currency: _string(json['currency']),
    availableQuantity: _int(
      json['available_quantity'] ?? json['quantity_available'],
    ),
    stockByLocation: _stockByLocation(json),
  );
}

class InventoryLocation {
  const InventoryLocation({required this.id, required this.name, this.code});

  final String id;
  final String name;
  final String? code;

  factory InventoryLocation.fromJson(Map<String, dynamic> json) =>
      InventoryLocation(
        id: json['id'].toString(),
        name: _string(json['name']) ?? 'Location',
        code: _string(json['code']),
      );
}

class InventoryLocationStock {
  const InventoryLocationStock({
    required this.locationId,
    required this.availableQuantity,
    this.locationName,
    this.locationCode,
  });

  final String locationId;
  final String? locationName;
  final String? locationCode;
  final int availableQuantity;

  String get displayLocation {
    final code = locationCode;
    if (locationName == null || locationName!.isEmpty) {
      return code == null || code.isEmpty ? locationId : code;
    }
    return code == null || code.isEmpty
        ? locationName!
        : '$locationName ($code)';
  }

  factory InventoryLocationStock.fromJson(Map<String, dynamic> json) {
    final location = (json['location'] as Map?)?.cast<String, dynamic>();
    return InventoryLocationStock(
      locationId:
          json['location_id']?.toString() ??
          location?['id']?.toString() ??
          'location',
      locationName:
          _string(json['location_name']) ??
          _string(json['name']) ??
          _string(location?['name']),
      locationCode:
          _string(json['location_code']) ?? _string(location?['code']),
      availableQuantity:
          _int(
            json['available_quantity'] ??
                json['quantity_available'] ??
                json['quantity'],
          ) ??
          0,
    );
  }
}

class MaterialRequestItemDraft {
  const MaterialRequestItemDraft({
    required this.item,
    required this.quantity,
    this.notes,
  });

  final InventoryItem item;
  final int quantity;
  final String? notes;

  Map<String, dynamic> toJson() => {
    'item_id': item.id,
    'quantity': quantity,
    if (notes != null && notes!.trim().isNotEmpty) 'notes': notes!.trim(),
  };
}

class MaterialRequestItem {
  const MaterialRequestItem({
    required this.id,
    required this.itemId,
    required this.quantity,
    this.itemName,
    this.notes,
    this.approvedQuantity,
    this.issuedQuantity,
    this.fulfilledQuantity,
  });

  final String id;
  final String itemId;
  final int quantity;
  final String? itemName;
  final String? notes;
  final int? approvedQuantity;
  final int? issuedQuantity;
  final int? fulfilledQuantity;

  factory MaterialRequestItem.fromJson(
    Map<String, dynamic> json,
  ) => MaterialRequestItem(
    id: json['id'].toString(),
    itemId: json['item_id'].toString(),
    quantity: _int(json['quantity']) ?? 0,
    itemName:
        _string(json['item_name']) ??
        (json['item'] is Map ? _string((json['item'] as Map)['name']) : null),
    notes: _string(json['notes']),
    approvedQuantity: _int(json['approved_quantity']),
    issuedQuantity: _int(json['issued_quantity'] ?? json['quantity_issued']),
    fulfilledQuantity: _int(
      json['fulfilled_quantity'] ?? json['quantity_fulfilled'],
    ),
  );
}

class MaterialRequest {
  const MaterialRequest({
    required this.id,
    required this.status,
    this.number,
    this.priority,
    this.notes,
    this.workOrderId,
    this.projectId,
    this.ticketId,
    this.sourceLocationId,
    this.sourceLocationName,
    this.destinationLocationId,
    this.destinationLocationName,
    this.approvalNotes,
    this.rejectionReason,
    this.issueNotes,
    this.createdAt,
    this.submittedAt,
    this.approvedAt,
    this.rejectedAt,
    this.issuedAt,
    this.fulfilledAt,
    this.supportSystem,
    this.supportReference,
    this.supportStatus,
    this.items = const [],
  });

  final String id;
  final String status;
  final String? number;
  final String? priority;
  final String? notes;
  final String? workOrderId;
  final String? projectId;
  final String? ticketId;
  final String? sourceLocationId;
  final String? sourceLocationName;
  final String? destinationLocationId;
  final String? destinationLocationName;
  final String? approvalNotes;
  final String? rejectionReason;
  final String? issueNotes;
  final DateTime? createdAt;
  final DateTime? submittedAt;
  final DateTime? approvedAt;
  final DateTime? rejectedAt;
  final DateTime? issuedAt;
  final DateTime? fulfilledAt;
  final String? supportSystem;
  final String? supportReference;
  final String? supportStatus;
  final List<MaterialRequestItem> items;

  factory MaterialRequest.fromJson(
    Map<String, dynamic> json,
  ) => MaterialRequest(
    id: json['id'].toString(),
    status: json['status'] as String? ?? 'draft',
    number: _string(json['number']),
    priority: _string(json['priority']),
    notes: _string(json['notes']),
    workOrderId: json['work_order_id']?.toString(),
    projectId: json['project_id']?.toString(),
    ticketId: json['ticket_id']?.toString(),
    sourceLocationId: _locationId(json, 'source'),
    sourceLocationName: _locationName(json, 'source'),
    destinationLocationId: _locationId(json, 'destination'),
    destinationLocationName: _locationName(json, 'destination'),
    approvalNotes:
        _string(json['approval_notes']) ?? _string(json['approved_notes']),
    rejectionReason:
        _string(json['rejection_reason']) ??
        _string(json['rejected_reason']) ??
        _string(json['rejection_notes']),
    issueNotes: _string(json['issue_notes']) ?? _string(json['issued_notes']),
    createdAt: _date(json['created_at']),
    submittedAt: _date(json['submitted_at']),
    approvedAt: _date(json['approved_at']),
    rejectedAt: _date(json['rejected_at']),
    issuedAt: _date(json['issued_at']),
    fulfilledAt: _date(json['fulfilled_at']),
    supportSystem: _string(json['support_system']),
    supportReference: _string(json['support_reference']),
    supportStatus: _string(json['support_status']),
    items: _mapList(json['items']).map(MaterialRequestItem.fromJson).toList(),
  );

  String get displayNumber => number ?? id;

  String? get sourceLocationLabel => sourceLocationName ?? sourceLocationId;

  String? get destinationLocationLabel =>
      destinationLocationName ?? destinationLocationId;
}

int? _int(Object? value) => switch (value) {
  int() => value,
  num() => value.toInt(),
  String() => int.tryParse(value),
  _ => null,
};

String? _string(Object? value) => value?.toString();

double? _double(Object? value) => switch (value) {
  num() => value.toDouble(),
  String() => double.tryParse(value),
  _ => null,
};

DateTime? _date(Object? value) =>
    value is String ? DateTime.tryParse(value) : null;

List<InventoryLocationStock> _stockByLocation(Map<String, dynamic> json) {
  final raw =
      json['stock_by_location'] ??
      json['location_stock'] ??
      json['stocks'] ??
      json['locations'];
  if (raw is! List) return const [];
  return raw
      .whereType<Map>()
      .map(
        (item) => InventoryLocationStock.fromJson(item.cast<String, dynamic>()),
      )
      .toList();
}

String? _locationId(Map<String, dynamic> json, String prefix) {
  final location = (json['${prefix}_location'] as Map?)
      ?.cast<String, dynamic>();
  return json['${prefix}_location_id']?.toString() ??
      location?['id']?.toString();
}

String? _locationName(Map<String, dynamic> json, String prefix) {
  final location = (json['${prefix}_location'] as Map?)
      ?.cast<String, dynamic>();
  return _string(json['${prefix}_location_name']) ??
      _string(location?['name']) ??
      _string(location?['code']);
}

List<Map<String, dynamic>> _mapList(Object? raw) {
  if (raw is! List) return const [];
  return [
    for (final item in raw)
      if (item is Map) item.cast<String, dynamic>(),
  ];
}

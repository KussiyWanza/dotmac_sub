class ExpenseCategory {
  const ExpenseCategory({
    required this.categoryCode,
    required this.categoryName,
    this.requiresReceipt = false,
    this.maxAmountPerClaim,
  });

  final String categoryCode;
  final String categoryName;
  final bool requiresReceipt;
  final double? maxAmountPerClaim;

  String get displayName => categoryName.isEmpty ? categoryCode : categoryName;

  factory ExpenseCategory.fromJson(Map<String, dynamic> json) =>
      ExpenseCategory(
        categoryCode: json['category_code']?.toString() ?? '',
        categoryName:
            _string(json['category_name']) ??
            json['category_code']?.toString() ??
            'Category',
        requiresReceipt: json['requires_receipt'] == true,
        maxAmountPerClaim: _double(json['max_amount_per_claim']),
      );
}

class ExpenseItemDraft {
  const ExpenseItemDraft({
    required this.categoryCode,
    required this.description,
    required this.amount,
    this.categoryName,
    this.expenseDate,
    this.vendorName,
    this.receiptUrl,
    this.notes,
  });

  final String categoryCode;
  final String? categoryName;
  final String description;
  final double amount;
  final String? expenseDate;
  final String? vendorName;
  final String? receiptUrl;
  final String? notes;

  Map<String, dynamic> toJson() => {
    'category_code': categoryCode,
    if (categoryName != null && categoryName!.trim().isNotEmpty)
      'category_name': categoryName!.trim(),
    'description': description,
    'amount': amount.toStringAsFixed(2),
    if (expenseDate != null && expenseDate!.trim().isNotEmpty)
      'expense_date': expenseDate!.trim(),
    if (vendorName != null && vendorName!.trim().isNotEmpty)
      'vendor_name': vendorName!.trim(),
    if (receiptUrl != null && receiptUrl!.trim().isNotEmpty)
      'receipt_url': receiptUrl!.trim(),
    if (notes != null && notes!.trim().isNotEmpty) 'notes': notes!.trim(),
  };
}

class ExpenseRequestItem {
  const ExpenseRequestItem({
    required this.id,
    required this.categoryCode,
    required this.amount,
    this.categoryName,
    this.description,
    this.expenseDate,
    this.vendorName,
    this.receiptUrl,
    this.notes,
    this.createdAt,
  });

  final String id;
  final String categoryCode;
  final double amount;
  final String? categoryName;
  final String? description;
  final String? expenseDate;
  final String? vendorName;
  final String? receiptUrl;
  final String? notes;
  final DateTime? createdAt;

  String get categoryLabel => categoryName == null || categoryName!.isEmpty
      ? categoryCode
      : categoryName!;

  factory ExpenseRequestItem.fromJson(Map<String, dynamic> json) =>
      ExpenseRequestItem(
        id: json['id'].toString(),
        categoryCode: json['category_code']?.toString() ?? '',
        amount: _double(json['amount']) ?? 0,
        categoryName: _string(json['category_name']),
        description: _string(json['description']),
        expenseDate: _string(json['expense_date']),
        vendorName: _string(json['vendor_name']),
        receiptUrl: _string(json['receipt_url']),
        notes: _string(json['notes']),
        createdAt: _date(json['created_at']),
      );
}

class ExpenseRequest {
  const ExpenseRequest({
    required this.id,
    required this.status,
    this.number,
    this.purpose,
    this.expenseDate,
    this.currency,
    this.notes,
    this.rejectionReason,
    this.erpClaimNumber,
    this.erpClaimStatus,
    this.erpSyncStatus,
    this.erpSyncError,
    this.total,
    this.ticketId,
    this.projectId,
    this.workOrderId,
    this.submittedAt,
    this.approvedAt,
    this.rejectedAt,
    this.paidAt,
    this.createdAt,
    this.updatedAt,
    this.items = const [],
  });

  final String id;
  final String status;
  final String? number;
  final String? purpose;
  final String? expenseDate;
  final String? currency;
  final String? notes;
  final String? rejectionReason;
  final String? erpClaimNumber;
  final String? erpClaimStatus;
  final String? erpSyncStatus;
  final String? erpSyncError;
  final double? total;
  final String? ticketId;
  final String? projectId;
  final String? workOrderId;
  final DateTime? submittedAt;
  final DateTime? approvedAt;
  final DateTime? rejectedAt;
  final DateTime? paidAt;
  final DateTime? createdAt;
  final DateTime? updatedAt;
  final List<ExpenseRequestItem> items;

  factory ExpenseRequest.fromJson(Map<String, dynamic> json) => ExpenseRequest(
    id: json['id'].toString(),
    status: json['status'] as String? ?? 'draft',
    number: _string(json['number']),
    purpose: _string(json['purpose']),
    expenseDate: _string(json['expense_date']),
    currency: _string(json['currency']),
    notes: _string(json['notes']),
    rejectionReason: _string(json['rejection_reason']),
    erpClaimNumber: _string(
      json['expense_claim_number'] ?? json['erp_claim_number'],
    ),
    erpClaimStatus: _string(
      json['expense_claim_status'] ?? json['erp_claim_status'],
    ),
    erpSyncStatus: _string(json['erp_sync_status'] ?? json['expense_system']),
    erpSyncError: _string(json['erp_sync_error']),
    total: _double(json['total_amount']),
    ticketId: json['ticket_id']?.toString(),
    projectId: json['project_id']?.toString(),
    workOrderId: json['work_order_id']?.toString(),
    submittedAt: _date(json['submitted_at']),
    approvedAt: _date(json['approved_at']),
    rejectedAt: _date(json['rejected_at']),
    paidAt: _date(json['paid_at']),
    createdAt: _date(json['created_at']),
    updatedAt: _date(json['updated_at']),
    items: _mapList(json['items']).map(ExpenseRequestItem.fromJson).toList(),
  );

  String get displayNumber =>
      number ?? (id.length > 8 ? id.substring(0, 8) : id);

  double get totalAmount =>
      total ?? items.fold<double>(0, (sum, item) => sum + item.amount);

  String get statusLabel => status.replaceAll('_', ' ');
}

String? _string(Object? value) => value?.toString();

double? _double(Object? value) => switch (value) {
  num() => value.toDouble(),
  String() => double.tryParse(value),
  _ => null,
};

DateTime? _date(Object? value) =>
    value is String ? DateTime.tryParse(value) : null;

List<Map<String, dynamic>> _mapList(Object? raw) {
  if (raw is! List) return const [];
  return [
    for (final item in raw)
      if (item is Map) item.cast<String, dynamic>(),
  ];
}

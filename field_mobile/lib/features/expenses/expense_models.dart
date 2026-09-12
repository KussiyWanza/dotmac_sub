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

class ExpenseApprover {
  const ExpenseApprover({
    required this.erpEmployeeId,
    required this.systemUserId,
    required this.displayName,
    required this.email,
  });
  final String erpEmployeeId;
  final String systemUserId;
  final String displayName;
  final String email;
  factory ExpenseApprover.fromJson(Map<String, dynamic> json) =>
      ExpenseApprover(
        erpEmployeeId: json['erp_employee_id'].toString(),
        systemUserId: json['system_user_id'].toString(),
        displayName: json['display_name'].toString(),
        email: json['email'].toString(),
      );
  Map<String, dynamic> toJson() => {
    'erp_employee_id': erpEmployeeId,
    'system_user_id': systemUserId,
    'display_name': displayName,
    'email': email,
  };
}

class ExpenseBank {
  const ExpenseBank({required this.bankCode, required this.bankName});
  final String bankCode;
  final String bankName;
  factory ExpenseBank.fromJson(Map<String, dynamic> json) => ExpenseBank(
    bankCode: json['bank_code'].toString(),
    bankName: json['bank_name'].toString(),
  );
}

class ExpenseProfileDestination {
  const ExpenseProfileDestination({
    required this.available,
    this.bankCode,
    this.bankName,
    this.maskedAccountNumber,
    this.beneficiaryName,
  });
  final bool available;
  final String? bankCode;
  final String? bankName;
  final String? maskedAccountNumber;
  final String? beneficiaryName;
  factory ExpenseProfileDestination.fromJson(Map<String, dynamic> json) =>
      ExpenseProfileDestination(
        available: json['available'] == true,
        bankCode: _string(json['bank_code']),
        bankName: _string(json['bank_name']),
        maskedAccountNumber: _string(json['masked_account_number']),
        beneficiaryName: _string(json['beneficiary_name']),
      );
}

class ExpenseFormContext {
  const ExpenseFormContext({
    required this.approvers,
    required this.banks,
    required this.profileDestination,
  });
  final List<ExpenseApprover> approvers;
  final List<ExpenseBank> banks;
  final ExpenseProfileDestination profileDestination;
  factory ExpenseFormContext.fromJson(Map<String, dynamic> json) =>
      ExpenseFormContext(
        approvers: _mapList(
          json['approvers'],
        ).map(ExpenseApprover.fromJson).toList(),
        banks: _mapList(json['banks']).map(ExpenseBank.fromJson).toList(),
        profileDestination: ExpenseProfileDestination.fromJson(
          (json['profile_destination'] as Map).cast<String, dynamic>(),
        ),
      );
}

enum ExpensePaymentMode {
  erpProfile('erp_profile'),
  expenseOverride('expense_override');

  const ExpensePaymentMode(this.apiValue);

  final String apiValue;

  factory ExpensePaymentMode.fromApiValue(Object? value) => switch (value) {
    'erp_profile' => ExpensePaymentMode.erpProfile,
    'expense_override' => ExpensePaymentMode.expenseOverride,
    _ => throw FormatException('Unknown expense payment mode: $value'),
  };
}

class VerifiedExpenseDestination {
  const VerifiedExpenseDestination({
    required this.destinationToken,
    required this.mode,
    required this.bankCode,
    required this.bankName,
    required this.maskedAccountNumber,
    required this.verifiedBeneficiaryName,
    required this.verifiedAt,
    required this.expiresAt,
  });

  final String destinationToken;
  final ExpensePaymentMode mode;
  final String bankCode;
  final String bankName;
  final String maskedAccountNumber;
  final String verifiedBeneficiaryName;
  final DateTime verifiedAt;
  final DateTime expiresAt;

  factory VerifiedExpenseDestination.fromJson(Map<String, dynamic> json) =>
      VerifiedExpenseDestination(
        destinationToken: _requiredString(json, 'destination_token'),
        mode: ExpensePaymentMode.fromApiValue(json['mode']),
        bankCode: _requiredString(json, 'bank_code'),
        bankName: _requiredString(json, 'bank_name'),
        maskedAccountNumber: _requiredString(json, 'masked_account_number'),
        verifiedBeneficiaryName: _requiredString(
          json,
          'verified_beneficiary_name',
        ),
        verifiedAt: _requiredDate(json, 'verified_at'),
        expiresAt: _requiredDate(json, 'expires_at'),
      );

  Map<String, dynamic> toJson() => {
    'destination_token': destinationToken,
    'mode': mode.apiValue,
    'bank_code': bankCode,
    'bank_name': bankName,
    'masked_account_number': maskedAccountNumber,
    'verified_beneficiary_name': verifiedBeneficiaryName,
    'verified_at': verifiedAt.toUtc().toIso8601String(),
    'expires_at': expiresAt.toUtc().toIso8601String(),
  };
}

class ExpenseReceiptUploadResult {
  const ExpenseReceiptUploadResult({
    required this.attachmentId,
    required this.downloadPath,
  });

  final String attachmentId;
  final String downloadPath;

  factory ExpenseReceiptUploadResult.fromJson(Map<String, dynamic> json) {
    final attachmentId = json['id']?.toString().trim() ?? '';
    final downloadPath = json['download_path']?.toString().trim() ?? '';
    if (attachmentId.isEmpty || downloadPath.isEmpty) {
      throw const FormatException(
        'Receipt upload did not return an attachment ID and download path.',
      );
    }
    return ExpenseReceiptUploadResult(
      attachmentId: attachmentId,
      downloadPath: downloadPath,
    );
  }
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
    this.receiptAttachmentId,
    this.notes,
  });

  final String categoryCode;
  final String? categoryName;
  final String description;
  final double amount;
  final String? expenseDate;
  final String? vendorName;
  final String? receiptUrl;
  final String? receiptAttachmentId;
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
    if (receiptAttachmentId != null && receiptAttachmentId!.trim().isNotEmpty)
      'receipt_attachment_id': receiptAttachmentId!.trim(),
    if (notes != null && notes!.trim().isNotEmpty) 'notes': notes!.trim(),
  };

  Map<String, dynamic> toDraftJson() => {
    'category_code': categoryCode,
    'category_name': categoryName,
    'description': description,
    'amount': amount,
    'expense_date': expenseDate,
    'vendor_name': vendorName,
    'receipt_url': receiptUrl,
    'receipt_attachment_id': receiptAttachmentId,
    'notes': notes,
  };

  factory ExpenseItemDraft.fromDraftJson(Map<String, dynamic> json) =>
      ExpenseItemDraft(
        categoryCode: json['category_code'] as String? ?? '',
        categoryName: json['category_name'] as String?,
        description: json['description'] as String? ?? '',
        amount: switch (json['amount']) {
          num value => value.toDouble(),
          String value => double.tryParse(value) ?? 0,
          _ => 0,
        },
        expenseDate: json['expense_date'] as String?,
        vendorName: json['vendor_name'] as String?,
        receiptUrl: json['receipt_url'] as String?,
        receiptAttachmentId: json['receipt_attachment_id'] as String?,
        notes: json['notes'] as String?,
      );
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
    this.paymentStatus,
    this.paymentIntentId,
    this.paymentError,
    this.selectedApproverName,
    this.paymentDestinationMode,
    this.recipientBankName,
    this.maskedAccountNumber,
    this.verifiedBeneficiaryName,
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
  final String? paymentStatus;
  final String? paymentIntentId;
  final String? paymentError;
  final String? selectedApproverName;
  final String? paymentDestinationMode;
  final String? recipientBankName;
  final String? maskedAccountNumber;
  final String? verifiedBeneficiaryName;
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
    paymentStatus: _string(json['payment_status']),
    paymentIntentId: _string(json['payment_intent_id']),
    paymentError: _string(json['payment_error']),
    selectedApproverName: _string(json['selected_approver_name']),
    paymentDestinationMode: _string(json['payment_destination_mode']),
    recipientBankName: _string(json['recipient_bank_name']),
    maskedAccountNumber: _string(json['masked_account_number']),
    verifiedBeneficiaryName: _string(json['verified_beneficiary_name']),
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

String _requiredString(Map<String, dynamic> json, String key) {
  final value = _string(json[key])?.trim() ?? '';
  if (value.isEmpty) {
    throw FormatException('Expense payment response is missing $key.');
  }
  return value;
}

DateTime _requiredDate(Map<String, dynamic> json, String key) {
  final value = _requiredString(json, key);
  final parsed = DateTime.tryParse(value);
  if (parsed == null) {
    throw FormatException('Expense payment response has an invalid $key.');
  }
  return parsed;
}

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

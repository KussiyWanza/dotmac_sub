import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../auth/auth_state.dart';
import '../execution/execution_controller.dart';
import 'expense_models.dart';

class ExpensesRepository {
  const ExpensesRepository(this._ref);

  final Ref _ref;

  Future<List<ExpenseRequest>> fetchRequests({String? status}) async {
    final local = await _offlineExpenseRequests(_ref);
    try {
      final response = await _ref
          .read(apiClientProvider)
          .dio
          .get(
            '/api/v1/field/expense-requests',
            queryParameters: {
              if (status != null && status.trim().isNotEmpty)
                'status': status.trim(),
              'limit': 100,
            },
          );
      return [...local, ..._items(response.data).map(ExpenseRequest.fromJson)];
    } on DioException {
      if (local.isNotEmpty) return local;
      rethrow;
    }
  }

  Future<ExpenseRequest> fetchRequest(String id) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/expense-requests/$id');
    return ExpenseRequest.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<ExpenseRequest> submitRequest({
    required String purpose,
    required List<ExpenseItemDraft> items,
    required String clientRef,
    required String workOrderId,
    required ExpenseApprover selectedApprover,
    required VerifiedExpenseDestination paymentDestination,
    String? expenseDate,
    String? currency,
    String? notes,
    String? projectId,
    String? ticketId,
  }) async {
    if (clientRef.trim().isEmpty) {
      throw ArgumentError(
        'clientRef is required when submitting an expense request',
      );
    }
    if (workOrderId.trim().isEmpty) {
      throw ArgumentError(
        'workOrderId is required when submitting an expense request',
      );
    }
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post(
          '/api/v1/field/expense-requests/submit',
          data: buildExpenseRequestPayload(
            purpose: purpose,
            items: items,
            clientRef: clientRef,
            expenseDate: expenseDate,
            currency: currency,
            notes: notes,
            workOrderId: workOrderId,
            projectId: projectId,
            ticketId: ticketId,
            selectedApprover: selectedApprover,
            paymentDestination: paymentDestination,
          ),
        );
    return ExpenseRequest.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<ExpenseRequest> cancelRequest(String id) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post('/api/v1/field/expense-requests/$id/cancel');
    return ExpenseRequest.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<List<ExpenseCategory>> fetchCategories() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/expense-requests/categories');
    return _items(response.data).map(ExpenseCategory.fromJson).toList();
  }

  Future<ExpenseFormContext> fetchFormContext() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/field/expense-requests/form-context');
    return ExpenseFormContext.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<VerifiedExpenseDestination> verifyDestination({
    required String sourceClaimId,
    required ExpensePaymentMode mode,
    String? bankCode,
    String? accountNumber,
    String? beneficiaryName,
  }) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post(
          '/api/v1/field/expense-requests/payment-destination/verify',
          data: {
            'source_claim_id': sourceClaimId,
            'mode': mode.apiValue,
            if (mode == ExpensePaymentMode.expenseOverride)
              'bank_code': bankCode,
            if (mode == ExpensePaymentMode.expenseOverride)
              'account_number': accountNumber,
            if (mode == ExpensePaymentMode.expenseOverride)
              'beneficiary_name': beneficiaryName,
          },
        );
    return VerifiedExpenseDestination.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }

  Future<List<String>> fetchVendors({String query = '', int limit = 25}) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get(
          '/api/v1/field/expense-requests/vendors',
          queryParameters: {
            if (query.trim().isNotEmpty) 'q': query.trim(),
            'limit': limit,
          },
        );
    return _items(response.data)
        .map((item) => item['label']?.toString().trim() ?? '')
        .where((label) => label.isNotEmpty)
        .toList();
  }

  Future<ExpenseReceiptUploadResult> uploadReceipt({
    required String workOrderId,
    required String filePath,
    required String fileName,
    String? clientRef,
  }) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post(
          '/api/v1/field/expense-requests/receipts',
          data: FormData.fromMap({
            'work_order_id': workOrderId.trim(),
            if (clientRef != null && clientRef.trim().isNotEmpty)
              'client_ref': clientRef.trim(),
            'file': await MultipartFile.fromFile(filePath, filename: fileName),
          }),
        );
    return ExpenseReceiptUploadResult.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }
}

Future<List<ExpenseRequest>> _offlineExpenseRequests(Ref ref) async {
  try {
    final entries = await ref
        .read(syncServiceProvider)
        .offlineRequestHistory('expense_request');
    return [
      for (final entry in entries)
        ExpenseRequest.fromJson({
          ...entry.payload,
          'id': entry.clientRef,
          'number': 'Queued expense',
          'status': entry.status == 'conflict' ? 'sync failed' : 'queued',
          'client_ref': entry.clientRef,
          'created_at': entry.createdAt.toIso8601String(),
          'erp_sync_error': entry.lastError,
        }),
    ];
  } on UnimplementedError {
    return const [];
  }
}

Map<String, dynamic> buildExpenseRequestPayload({
  required String purpose,
  required List<ExpenseItemDraft> items,
  required String clientRef,
  required String workOrderId,
  required ExpenseApprover selectedApprover,
  required VerifiedExpenseDestination paymentDestination,
  String? expenseDate,
  String? currency,
  String? notes,
  String? projectId,
  String? ticketId,
}) => {
  'purpose': purpose.trim(),
  'client_ref': clientRef.trim(),
  if (expenseDate != null && expenseDate.trim().isNotEmpty)
    'expense_date': expenseDate.trim(),
  if (currency != null && currency.trim().isNotEmpty)
    'currency': currency.trim(),
  if (notes != null && notes.trim().isNotEmpty) 'notes': notes.trim(),
  'work_order_id': workOrderId.trim(),
  if (projectId != null && projectId.trim().isNotEmpty)
    'project_id': projectId.trim(),
  if (ticketId != null && ticketId.trim().isNotEmpty)
    'ticket_id': ticketId.trim(),
  'selected_approver': selectedApprover.toJson(),
  'payment_destination': paymentDestination.toJson(),
  'items': items.map((item) => item.toJson()).toList(),
};

List<Map<String, dynamic>> _items(Object? data) {
  if (data is Map && data['items'] is List) {
    return _mapItems(data['items']);
  }
  if (data is Map) {
    for (final key in ['data', 'results', 'expense_requests', 'requests']) {
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

final expensesRepositoryProvider = Provider<ExpensesRepository>(
  ExpensesRepository.new,
);

final expenseRequestsProvider = FutureProvider<List<ExpenseRequest>>(
  (ref) => ref.watch(expensesRepositoryProvider).fetchRequests(),
);

final expenseRequestProvider = FutureProvider.family<ExpenseRequest, String>(
  (ref, id) => ref.watch(expensesRepositoryProvider).fetchRequest(id),
);

final expenseCategoriesProvider = FutureProvider<List<ExpenseCategory>>(
  (ref) => ref.watch(expensesRepositoryProvider).fetchCategories(),
);

final expenseFormContextProvider = FutureProvider<ExpenseFormContext>(
  (ref) => ref.watch(expensesRepositoryProvider).fetchFormContext(),
);

final expenseVendorsProvider = FutureProvider<List<String>>(
  (ref) => ref.watch(expensesRepositoryProvider).fetchVendors(),
);

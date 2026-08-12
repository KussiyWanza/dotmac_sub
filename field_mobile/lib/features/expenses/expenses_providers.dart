import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../auth/auth_state.dart';
import 'expense_models.dart';

class ExpensesRepository {
  const ExpensesRepository(this._ref);

  final Ref _ref;

  Future<List<ExpenseRequest>> fetchRequests({String? status}) async {
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
    return _items(response.data).map(ExpenseRequest.fromJson).toList();
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

  Future<ExpenseRequest> createRequest({
    required String purpose,
    required List<ExpenseItemDraft> items,
    String? clientRef,
    String? expenseDate,
    String? currency,
    String? notes,
    String? workOrderId,
    String? projectId,
    String? ticketId,
    bool submit = true,
  }) async {
    if (submit && (clientRef == null || clientRef.trim().isEmpty)) {
      throw ArgumentError(
        'clientRef is required when submitting an expense request',
      );
    }
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .post(
          submit
              ? '/api/v1/field/expense-requests/submit'
              : '/api/v1/field/expense-requests',
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

  Future<String> uploadReceipt({
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
    final data = (response.data as Map).cast<String, dynamic>();
    final downloadPath = data['download_path']?.toString().trim();
    if (downloadPath != null && downloadPath.isNotEmpty) return downloadPath;
    final id = data['id']?.toString().trim();
    if (id != null && id.isNotEmpty) {
      return '/api/v1/field/attachments/$id/content';
    }
    throw StateError('Receipt upload did not return an attachment link.');
  }
}

Map<String, dynamic> buildExpenseRequestPayload({
  required String purpose,
  required List<ExpenseItemDraft> items,
  String? clientRef,
  String? expenseDate,
  String? currency,
  String? notes,
  String? workOrderId,
  String? projectId,
  String? ticketId,
}) => {
  'purpose': purpose.trim(),
  if (clientRef != null && clientRef.trim().isNotEmpty)
    'client_ref': clientRef.trim(),
  if (expenseDate != null && expenseDate.trim().isNotEmpty)
    'expense_date': expenseDate.trim(),
  if (currency != null && currency.trim().isNotEmpty)
    'currency': currency.trim(),
  if (notes != null && notes.trim().isNotEmpty) 'notes': notes.trim(),
  if (workOrderId != null && workOrderId.trim().isNotEmpty)
    'work_order_id': workOrderId.trim(),
  if (projectId != null && projectId.trim().isNotEmpty)
    'project_id': projectId.trim(),
  if (ticketId != null && ticketId.trim().isNotEmpty)
    'ticket_id': ticketId.trim(),
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

final expenseVendorsProvider = FutureProvider<List<String>>(
  (ref) => ref.watch(expensesRepositoryProvider).fetchVendors(),
);

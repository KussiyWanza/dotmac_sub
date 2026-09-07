import 'package:dotmac_field/features/manager/manager_providers.dart';
import 'package:dotmac_field/features/manager/manager_screen.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('approval result preserves the ERP delivery state and event id', () {
    final result = ExpenseApprovalResult.fromJson({
      'id': 'expense-1',
      'status': 'approved',
      'erp_sync_status': 'pending',
      'erp_sync_event_id': 'event-1',
    });

    expect(result.status, 'approved');
    expect(result.erpSyncStatus, 'pending');
    expect(result.erpSyncEventId, 'event-1');
    expect(expenseApprovalMessage(result), 'Expense approved; waiting for ERP');
  });

  test('approval message does not claim a failed delivery was synced', () {
    const result = ExpenseApprovalResult(
      id: 'expense-1',
      status: 'approved',
      erpSyncStatus: 'dead',
      erpSyncError: 'retry limit reached',
    );

    expect(
      expenseApprovalMessage(result),
      'Expense approved; ERP sync needs attention',
    );
  });

  test('approval message confirms an accepted ERP delivery', () {
    const result = ExpenseApprovalResult(
      id: 'expense-1',
      status: 'approved',
      erpSyncStatus: 'accepted',
    );

    expect(
      expenseApprovalMessage(result),
      'Expense approved and synced to ERP',
    );
  });
}

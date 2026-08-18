import 'dart:convert';

import 'package:drift/drift.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'database.dart';

const materialRequestDraftId = 'material_request:new';
const expenseRequestDraftId = 'expense_request:new';

class SavedDraft {
  const SavedDraft({
    required this.id,
    required this.payload,
    required this.updatedAt,
  });

  final String id;
  final Map<String, dynamic> payload;
  final DateTime updatedAt;
}

class DraftStore {
  const DraftStore(this.db);

  final AppDatabase? db;

  Future<Map<String, dynamic>?> load(String id) async {
    final database = db;
    if (database == null) return null;
    final row = await (database.select(
      database.draftEntries,
    )..where((entry) => entry.id.equals(id))).getSingleOrNull();
    if (row == null) return null;
    return (jsonDecode(row.payloadJson) as Map).cast<String, dynamic>();
  }

  Future<void> save({
    required String id,
    required String type,
    required Map<String, dynamic> payload,
  }) async {
    final database = db;
    if (database == null) return;
    final now = DateTime.now().toUtc();
    await database
        .into(database.draftEntries)
        .insertOnConflictUpdate(
          DraftEntriesCompanion.insert(
            id: id,
            type: type,
            payloadJson: jsonEncode(payload),
            updatedAt: now,
          ),
        );
  }

  Future<void> delete(String id) async {
    final database = db;
    if (database == null) return;
    await (database.delete(
      database.draftEntries,
    )..where((entry) => entry.id.equals(id))).go();
  }

  Future<List<SavedDraft>> list(String type) async {
    final database = db;
    if (database == null) return const [];
    final rows =
        await (database.select(database.draftEntries)
              ..where((entry) => entry.type.equals(type))
              ..orderBy([(entry) => OrderingTerm.desc(entry.updatedAt)]))
            .get();
    return rows
        .map(
          (row) => SavedDraft(
            id: row.id,
            payload: (jsonDecode(row.payloadJson) as Map)
                .cast<String, dynamic>(),
            updatedAt: row.updatedAt,
          ),
        )
        .toList();
  }
}

final draftStoreProvider = Provider<DraftStore>(
  (ref) => const DraftStore(null),
);

final materialRequestDraftsProvider = FutureProvider<List<SavedDraft>>(
  (ref) => ref.watch(draftStoreProvider).list('material_request'),
);

final expenseRequestDraftsProvider = FutureProvider<List<SavedDraft>>(
  (ref) => ref.watch(draftStoreProvider).list('expense_request'),
);

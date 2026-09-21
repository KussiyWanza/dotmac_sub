import 'dart:convert';

import 'package:drift/drift.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../secure/evidence_cipher.dart';
import '../secure/evidence_files.dart';
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

/// Drafts hold whatever the technician has typed but not yet submitted, which
/// is customer data the server has never seen. The payload column therefore
/// holds an AES-GCM envelope bound to the scope, not readable JSON, and every
/// query is scoped as well as encrypted.
///
/// A store with no database, cipher or scope is inert: it reads nothing and
/// writes nothing, which is what an unauthenticated launch gets.
class DraftStore {
  const DraftStore({this.db, this.cipher, this.scopeKey});

  final AppDatabase? db;
  final EvidenceCipher? cipher;
  final String? scopeKey;

  Future<Map<String, dynamic>?> load(String id) async {
    final database = db;
    final scope = scopeKey;
    if (database == null || scope == null) return null;
    return database.work.run(() async {
      final row =
          await (database.select(database.draftEntries)..where(
                (entry) => entry.scopeKey.equals(scope) & entry.id.equals(id),
              ))
              .getSingleOrNull();
      if (row == null) return null;
      return _decode(row.id, row.payloadJson);
    });
  }

  Future<void> save({
    required String id,
    required String type,
    required Map<String, dynamic> payload,
  }) async {
    final database = db;
    final envelope = cipher;
    final scope = scopeKey;
    if (database == null || envelope == null || scope == null) return;
    await database.work.run(() async {
      final now = DateTime.now().toUtc();
      await database
          .into(database.draftEntries)
          .insertOnConflictUpdate(
            DraftEntriesCompanion.insert(
              scopeKey: scope,
              id: id,
              type: type,
              payloadJson: envelope.sealText(
                jsonEncode(payload),
                context: evidenceContext(scope, 'draft', id),
              ),
              updatedAt: now,
            ),
          );
    });
  }

  Future<void> delete(String id) async {
    final database = db;
    final scope = scopeKey;
    if (database == null || scope == null) return;
    await database.work.run(() async {
      await (database.delete(database.draftEntries)..where(
            (entry) => entry.scopeKey.equals(scope) & entry.id.equals(id),
          ))
          .go();
    });
  }

  Future<List<SavedDraft>> list(String type) async {
    final database = db;
    final scope = scopeKey;
    if (database == null || scope == null) return const [];
    return database.work.run(() async {
      final rows =
          await (database.select(database.draftEntries)
                ..where(
                  (entry) =>
                      entry.scopeKey.equals(scope) & entry.type.equals(type),
                )
                ..orderBy([(entry) => OrderingTerm.desc(entry.updatedAt)]))
              .get();
      final drafts = <SavedDraft>[];
      for (final row in rows) {
        final payload = _decode(row.id, row.payloadJson);
        // An envelope that will not open belongs to a key we no longer hold.
        // It is skipped rather than surfaced: unreadable is not a draft.
        if (payload == null) continue;
        drafts.add(
          SavedDraft(id: row.id, payload: payload, updatedAt: row.updatedAt),
        );
      }
      return drafts;
    });
  }

  Map<String, dynamic>? _decode(String id, String envelope) {
    final opener = cipher;
    final scope = scopeKey;
    if (opener == null || scope == null) return null;
    try {
      final json = opener.openText(
        envelope,
        context: evidenceContext(scope, 'draft', id),
      );
      return (jsonDecode(json) as Map).cast<String, dynamic>();
    } on Object {
      return null;
    }
  }
}

final draftStoreProvider = Provider<DraftStore>((ref) => const DraftStore());

final materialRequestDraftsProvider = FutureProvider<List<SavedDraft>>(
  (ref) => ref.watch(draftStoreProvider).list('material_request'),
);

final expenseRequestDraftsProvider = FutureProvider<List<SavedDraft>>(
  (ref) => ref.watch(draftStoreProvider).list('expense_request'),
);

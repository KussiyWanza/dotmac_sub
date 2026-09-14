import 'package:drift/drift.dart';

import '../secure/store_work_gate.dart';

part 'database.g.dart';

/// Rebuildable read-model projections. Every row here is a copy of something
/// the server still owns, so a wipe destroys them outright and the plaintext
/// migration deliberately refuses to carry them across — re-fetching beats
/// re-encrypting a stale copy.
mixin RebuildableProjection on Table {}

/// Pending outbound evidence the server has NOT accepted yet: the only rows on
/// this device whose loss is unrecoverable. The plaintext migration carries
/// exactly these, and only these, into the encrypted store.
mixin PendingOutbound on Table {}

/// Every table carries the scope of the principal that owns its rows. This is
/// a structural partition, not a read-time filter: the column is part of the
/// primary key wherever the table declares one, so a row belonging to another
/// technician or another deployment cannot collide with — or be mistaken
/// for — one of ours. [ScopedRows] supplies the column; the encrypted store
/// also opens one database file per scope, so both mechanisms have to fail
/// together before foreign data can be read.
mixin ScopedRows on Table {
  TextColumn get scopeKey => text()();
}

/// Cached job snapshots: the list payload plus the full detail JSON so the
/// app works in coverage dead zones.
class CachedJobs extends Table with ScopedRows, RebuildableProjection {
  TextColumn get id => text()();
  TextColumn get title => text()();
  TextColumn get status => text()();
  TextColumn get workType => text()();
  TextColumn get priority => text()();
  DateTimeColumn get scheduledStart => dateTime().nullable()();
  TextColumn get detailJson => text().nullable()();
  DateTimeColumn get cachedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {scopeKey, id};
}

class CachedScheduleEntries extends Table
    with ScopedRows, RebuildableProjection {
  TextColumn get referenceId => text()();
  TextColumn get type => text()();
  DateTimeColumn get startAt => dateTime()();
  DateTimeColumn get endAt => dateTime().nullable()();
  TextColumn get title => text()();

  @override
  Set<Column> get primaryKey => {scopeKey, referenceId, startAt};
}

class CachedMapAssets extends Table with ScopedRows, RebuildableProjection {
  TextColumn get assetType => text()();
  TextColumn get assetId => text()();
  TextColumn get title => text()();
  TextColumn get subtitle => text().nullable()();
  RealColumn get latitude => real()();
  RealColumn get longitude => real()();
  TextColumn get status => text().nullable()();
  DateTimeColumn get updatedAt => dateTime().nullable()();
  DateTimeColumn get cachedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {scopeKey, assetType, assetId};
}

class CachedMapAssetSyncCursors extends Table
    with ScopedRows, RebuildableProjection {
  TextColumn get assetType => text()();
  DateTimeColumn get syncedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {scopeKey, assetType};
}

/// Immutable work-order fiber evidence snapshots. The composite identity keeps
/// offline evidence bound to the authenticated principal, native job, and exact
/// server report; callers must never cross principals/jobs or overwrite a hash.
class CachedWorkOrderEvidenceMaps extends Table
    with ScopedRows, RebuildableProjection {
  TextColumn get principalScope => text()();
  TextColumn get workOrderPublicId => text()();
  TextColumn get reportSha256 => text()();
  TextColumn get sourceOverlaySha256 => text()();
  TextColumn get payloadJson => text()();
  DateTimeColumn get cachedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {
    scopeKey,
    principalScope,
    workOrderPublicId,
    reportSha256,
  };
}

/// Queued offline mutations, flushed FIFO. `clientRef` doubles as the
/// server-side idempotency key (client_event_id / client_ref).
class OutboxEntries extends Table with ScopedRows, PendingOutbound {
  IntColumn get seq => integer().autoIncrement()();
  TextColumn get clientRef => text().unique()();
  TextColumn get kind =>
      text()(); // transition|note|worklog|material_consume|expense_request
  TextColumn get payloadJson => text()();
  TextColumn get status =>
      text().withDefault(const Constant('pending'))(); // pending|sent|conflict
  IntColumn get attempts => integer().withDefault(const Constant(0))();
  TextColumn get lastError => text().nullable()();
  DateTimeColumn get createdAt => dateTime()();
}

/// Photos captured offline, uploaded by the sync service. `localPath` points at
/// an AES-GCM envelope inside the scope's evidence directory, never at a
/// readable JPEG.
class PendingPhotos extends Table with ScopedRows, PendingOutbound {
  TextColumn get clientRef => text()();
  TextColumn get localPath => text()();
  TextColumn get kind => text().withDefault(const Constant('photo'))();
  TextColumn get workOrderId => text().nullable()();
  TextColumn get installationProjectId => text().nullable()();
  RealColumn get latitude => real().nullable()();
  RealColumn get longitude => real().nullable()();
  DateTimeColumn get capturedAt => dateTime()();
  BoolColumn get uploaded => boolean().withDefault(const Constant(false))();
  // Terminal rejection (permanent 4xx) — excluded from upload retries and
  // surfaced in the Profile conflict-review list. Distinct from `uploaded`.
  BoolColumn get failed => boolean().withDefault(const Constant(false))();
  TextColumn get lastError => text().nullable()();

  @override
  Set<Column> get primaryKey => {scopeKey, clientRef};
}

/// Local form drafts that have not been submitted yet. These are not synced
/// directly; the relevant form reloads them and submits through the normal API.
/// `payloadJson` holds an AES-GCM envelope, not readable JSON.
class DraftEntries extends Table with ScopedRows, PendingOutbound {
  TextColumn get id => text()();
  TextColumn get type => text()(); // material_request|expense_request
  TextColumn get payloadJson => text()();
  DateTimeColumn get updatedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {scopeKey, id};
}

@DriftDatabase(
  tables: [
    CachedJobs,
    CachedScheduleEntries,
    CachedMapAssets,
    CachedMapAssetSyncCursors,
    CachedWorkOrderEvidenceMaps,
    OutboxEntries,
    PendingPhotos,
    DraftEntries,
  ],
)
class AppDatabase extends _$AppDatabase {
  AppDatabase(super.executor, {StoreWorkGate? work})
    : work = work ?? StoreWorkGate();

  /// Coordinates every asynchronous operation that can retain this connection
  /// across a session ending.
  final StoreWorkGate work;

  @override
  int get schemaVersion => 6;

  /// Tables the server can rebuild. A wipe drops them; the plaintext migration
  /// never carries them.
  Iterable<TableInfo<Table, dynamic>> get rebuildableProjections =>
      allTables.where(_declaresRebuildable);

  /// Tables holding evidence the server has not accepted yet. Losing a row here
  /// loses a technician's work.
  Iterable<TableInfo<Table, dynamic>> get pendingOutbound =>
      allTables.where(_declaresPendingOutbound);

  // Named predicates rather than `whereType`: callers need the TableInfo back,
  // and whereType would narrow the element type to the marker mixin and lose
  // the table with it.
  static bool _declaresRebuildable(TableInfo<Table, dynamic> table) =>
      table is RebuildableProjection;

  static bool _declaresPendingOutbound(TableInfo<Table, dynamic> table) =>
      table is PendingOutbound;

  @override
  MigrationStrategy get migration => MigrationStrategy(
    onUpgrade: (m, from, to) async {
      if (from < 6) {
        // Unreachable by construction. Schema 6 introduced the scope column and
        // the composite primary keys, and it shipped together with the move to
        // a per-scope SQLCipher file — a database at this path is always
        // created at 6 or later. The plaintext store that predates it lives at
        // the legacy path and is carried across by PlaintextOfflineMigration,
        // which re-encrypts unsent evidence row by row. Failing loudly here
        // keeps an unexpected pre-6 file readable for recovery instead of
        // silently rebuilding it and destroying whatever it held.
        throw StateError(
          'Encrypted offline store cannot predate schema 6 (found $from)',
        );
      }
      if (from < 2) {
        await m.addColumn(pendingPhotos, pendingPhotos.failed);
      }
      if (from < 3) {
        await m.createTable(cachedMapAssets);
        await m.createTable(cachedMapAssetSyncCursors);
      }
      if (from < 4) {
        await m.createTable(draftEntries);
      }
      if (from < 5) {
        await m.createTable(cachedWorkOrderEvidenceMaps);
      }
    },
  );
}

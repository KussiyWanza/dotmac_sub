import 'dart:io';

import 'package:path/path.dart' as p;

import '../offline/database.dart';
import 'data_scope.dart';
import 'encrypted_database.dart';
import 'evidence_cipher.dart';
import 'evidence_files.dart';
import 'scope_key_ring.dart';
import 'store_work_gate.dart';

/// Everything one principal's offline data lives in: an encrypted database, an
/// encrypted evidence directory, and the keys that open them. Immutable and
/// bound to exactly one [DataScope]: rebinding builds a new store rather than
/// re-pointing this one, so no object can ever be half-way between two
/// principals.
class SecureFieldStore {
  SecureFieldStore({
    required this.scope,
    required this.database,
    required this.root,
    required this.evidence,
    required this.cipher,
  });

  final DataScope scope;
  final AppDatabase database;
  final Directory root;
  final EvidenceFiles evidence;
  final EvidenceCipher cipher;

  String get scopeKey => scope.key;

  StoreWorkGate get work => database.work;

  File get databaseFile => File(p.join(root.path, databaseFileName));

  /// The queued location pings. An envelope, not JSON.
  File get locationQueueFile => File(p.join(root.path, 'location_queue.bin'));

  static const databaseFileName = 'field.sqlite';
  static const evidenceDirectoryName = 'evidence';

  /// Refuses any new operation before session credentials are cleared.
  void stopAcceptingWork() => work.stopAccepting();

  /// Kills the store after every operation admitted before teardown has
  /// finished. No stale task can retain this database connection across close.
  Future<void> discardAndClose() async {
    stopAcceptingWork();
    await work.waitUntilDrained();
    evidence.discard();
    await database.close();
  }
}

/// Builds a [SecureFieldStore] for a scope, generating its keys on first use.
class SecureStoreOpener {
  SecureStoreOpener({
    required this.documents,
    required this.keyRing,
    EncryptedDatabaseFactory? openDatabase,
  }) : openDatabase = openDatabase ?? openEncryptedDatabase;

  final Directory documents;
  final ScopeKeyRing keyRing;
  final EncryptedDatabaseFactory openDatabase;

  Directory get scopesRoot => Directory(p.join(documents.path, 'scopes'));

  Directory scopeRoot(String scopeKey) =>
      Directory(p.join(scopesRoot.path, scopeKey));

  Future<SecureFieldStore> open(DataScope scope) async {
    if (!scope.isBound) {
      throw ArgumentError('refusing to open a store for the unbound scope');
    }
    final keys = await keyRing.loadOrCreate(scope);
    final root = scopeRoot(scope.key);
    await root.create(recursive: true);
    final database = openDatabase(
      File(p.join(root.path, SecureFieldStore.databaseFileName)),
      keys.databaseKeyHex,
    );
    // Belt and braces: one database per scope should make this a no-op, but a
    // foreign row is data we must never read, so every open sweeps for one.
    await purgeForeignScopeRows(database, scope.key);
    final cipher = EvidenceCipher(keys.evidenceKey);
    return SecureFieldStore(
      scope: scope,
      database: database,
      root: root,
      cipher: cipher,
      evidence: EvidenceFiles(
        directory: Directory(
          p.join(root.path, SecureFieldStore.evidenceDirectoryName),
        ),
        cipher: cipher,
        scopeKey: scope.key,
        work: database.work,
      ),
    );
  }
}

import 'dart:convert';
import 'dart:io';

import 'package:path/path.dart' as p;

import '../api/token_store.dart';
import 'scope_key_ring.dart';
import 'secure_field_store.dart';

/// Why local data is being destroyed.
///
/// The first three are session triggers: explicit logout, an authoritative
/// token revocation, and an account switch. They all destroy exactly the same
/// things, because a partial wipe is not a wipe. [orphanedScope] is the startup
/// reconciler sweeping a principal who is no longer signed in: it destroys
/// the same artefacts but must not touch the current session's tokens.
enum WipeTrigger { explicitLogout, tokenRevoked, accountSwitch, orphanedScope }

extension WipeTriggerSession on WipeTrigger {
  bool get endsTheSession => this != WipeTrigger.orphanedScope;
}

class WipeRequest {
  const WipeRequest({required this.scopeKey, required this.trigger});

  /// The scope whose data is being destroyed. Empty when the session ended
  /// before a scope could be resolved from the token: the session artefacts
  /// still go, and the startup reconciler sweeps whatever scopes remain.
  final String scopeKey;
  final WipeTrigger trigger;

  Map<String, dynamic> toJson() => {'scope': scopeKey, 'trigger': trigger.name};

  static WipeRequest? fromJson(Object? json) {
    if (json is! Map) return null;
    final scope = json['scope']?.toString();
    if (scope == null || scope.isEmpty) return null;
    final name = json['trigger']?.toString();
    return WipeRequest(
      scopeKey: scope,
      // An unrecognised trigger is treated as a session trigger: doing more
      // destruction than the interrupted run intended is the safe direction.
      trigger: WipeTrigger.values.firstWhere(
        (trigger) => trigger.name == name,
        orElse: () => WipeTrigger.tokenRevoked,
      ),
    );
  }

  @override
  bool operator ==(Object other) =>
      other is WipeRequest &&
      other.scopeKey == scopeKey &&
      other.trigger == trigger;

  @override
  int get hashCode => Object.hash(scopeKey, trigger);

  @override
  String toString() => 'WipeRequest($scopeKey, ${trigger.name})';
}

/// The one destruction path.
///
/// Explicit logout, an authoritative token revocation and an account switch all
/// call [wipe] with a different [WipeTrigger] and nothing else different. There
/// is deliberately no second implementation for any of them to drift away from,
/// and no argument that turns part of the wipe off.
abstract class OfflineWipe {
  Future<void> wipe(WipeRequest request, {SecureFieldStore? live});

  /// Finishes any wipe interrupted by a crash or a kill. Called at every
  /// launch, before anything is allowed to read local storage.
  Future<List<WipeRequest>> resumeInterrupted();
}

/// File-journalled wipe.
///
/// The order is the whole design:
///
/// 1. journal the intent, so a crash at any later point is recoverable;
/// 2. refuse new work against the live store;
/// 3. clear the session tokens, so nothing can authenticate as this principal;
/// 4. drain admitted work and close the live database connection;
/// 5. destroy the scope's keys, after which every byte still on disk is
///    ciphertext nobody can open;
/// 6. delete the scope directory, and any legacy plaintext residue;
/// 7. clear the journal entry.
///
/// Because step 5 precedes step 6, an interruption cannot leave readable
/// residue. It can only leave unopenable files for the next launch to sweep.
class ScopedOfflineWipe implements OfflineWipe {
  ScopedOfflineWipe({
    required this.documents,
    required this.keyRing,
    required this.tokenStore,
    required this.legacyPaths,
  });

  static const journalFileName = '.wipe_journal.json';

  final Directory documents;
  final ScopeKeyRing keyRing;
  final TokenStore tokenStore;
  final LegacyPlaintextPaths legacyPaths;

  Directory get _scopesRoot => Directory(p.join(documents.path, 'scopes'));

  File get journalFile => File(p.join(documents.path, journalFileName));

  @override
  Future<void> wipe(WipeRequest request, {SecureFieldStore? live}) async {
    await _journal([...await _pending(), request]);
    await _destroy(request, live: live);
    await _forget(request.scopeKey);
  }

  @override
  Future<List<WipeRequest>> resumeInterrupted() async {
    final pending = await _pending();
    for (final request in pending) {
      await _destroy(request);
      await _forget(request.scopeKey);
    }
    return pending;
  }

  Future<void> _destroy(WipeRequest request, {SecureFieldStore? live}) async {
    final liveStore = live != null && live.scopeKey == request.scopeKey
        ? live
        : null;
    liveStore?.stopAcceptingWork();
    if (request.trigger.endsTheSession) await tokenStore.clear();
    await liveStore?.discardAndClose();
    if (request.scopeKey.isNotEmpty) {
      await keyRing.destroy(request.scopeKey);
      await _deleteTree(Directory(p.join(_scopesRoot.path, request.scopeKey)));
    }
    if (request.trigger.endsTheSession) await legacyPaths.destroy();
  }

  Future<List<WipeRequest>> _pending() async {
    if (!await journalFile.exists()) return const [];
    try {
      final decoded = jsonDecode(await journalFile.readAsString());
      if (decoded is! List) {
        throw const FormatException('journal is not a list');
      }
      return [for (final entry in decoded) ?WipeRequest.fromJson(entry)];
    } on Object {
      // An unreadable journal must not strand a wipe. We cannot tell which
      // scope it named, so sweep every scope this device still holds keys for.
      return [
        for (final scope in await keyRing.knownScopeKeys())
          WipeRequest(scopeKey: scope, trigger: WipeTrigger.tokenRevoked),
      ];
    }
  }

  Future<void> _journal(List<WipeRequest> pending) async {
    await documents.create(recursive: true);
    await journalFile.writeAsString(
      jsonEncode([for (final request in pending) request.toJson()]),
      flush: true,
    );
  }

  Future<void> _forget(String scopeKey) async {
    final remaining = [
      for (final request in await _pending())
        if (request.scopeKey != scopeKey) request,
    ];
    if (remaining.isEmpty) {
      if (await journalFile.exists()) await journalFile.delete();
      return;
    }
    await _journal(remaining);
  }

  Future<void> _deleteTree(Directory directory) async {
    if (!await directory.exists()) return;
    try {
      await directory.delete(recursive: true);
    } on FileSystemException {
      // Whatever survives is ciphertext with no key: unreadable, and the next
      // launch retries it from the journal.
    }
  }
}

/// The unencrypted storage the field app used before this change. It belongs to
/// no scope, so every session wipe destroys it rather than one of them.
class LegacyPlaintextPaths {
  LegacyPlaintextPaths(this.documents);

  static const databaseFileName = 'dotmac_field.sqlite';
  static const photoDirectoryName = 'field_photos';
  static const locationQueueFileName = 'pending_location_pings.json';

  final Directory documents;

  File get database => File(p.join(documents.path, databaseFileName));

  Directory get photos => Directory(p.join(documents.path, photoDirectoryName));

  File get locationQueue => File(p.join(documents.path, locationQueueFileName));

  Future<bool> get exists async =>
      await database.exists() ||
      await photos.exists() ||
      await locationQueue.exists();

  Future<void> destroy() async {
    for (final path in [
      database.path,
      '${database.path}-wal',
      '${database.path}-shm',
      locationQueue.path,
      '${locationQueue.path}.tmp',
      '${locationQueue.path}.corrupt',
    ]) {
      final file = File(path);
      if (await file.exists()) await file.delete();
    }
    if (await photos.exists()) await photos.delete(recursive: true);
  }
}

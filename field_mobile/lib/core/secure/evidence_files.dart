import 'dart:io';
import 'dart:typed_data';

import 'package:path/path.dart' as p;

import 'evidence_cipher.dart';
import 'store_work_gate.dart';

/// The authenticated-data context an envelope is bound to.
///
/// Shared by every writer — evidence files, draft payloads, queued mutations
/// and the location queue — so the runtime and the plaintext migration cannot
/// disagree about what an envelope is bound to. It always starts with the scope
/// key, which is what stops one principal's envelope opening under another's.
String evidenceContext(String scopeKey, String purpose, String reference) =>
    '$scopeKey/$purpose/$reference';

/// Scope-bound, encrypted storage for evidence files: photos, signatures and
/// the queued location payloads.
///
/// Every file lives under the scope's own directory and every envelope is bound
/// to the scope key, so a file copied out of one technician's storage is
/// unreadable anywhere else. Every write re-checks the discard flag after each
/// suspension point, which is what makes "logout cannot recreate deleted state"
/// an invariant rather than a race.
class EvidenceFiles {
  EvidenceFiles({
    required this.directory,
    required this.cipher,
    required this.scopeKey,
    required this.work,
  });

  final Directory directory;
  final EvidenceCipher cipher;
  final String scopeKey;
  final StoreWorkGate work;

  bool _discarded = false;

  bool get isDiscarded => _discarded;

  /// Marks the store dead. Called by the wipe before it deletes anything.
  void discard() => _discarded = true;

  String contextFor(String purpose, String reference) =>
      evidenceContext(scopeKey, purpose, reference);

  File fileNamed(String name) => File(p.join(directory.path, name));

  Future<File> write(
    String name,
    List<int> plaintext, {
    required String purpose,
    required String reference,
  }) => work.run(() async {
    _refuse('write $purpose');
    await directory.create(recursive: true);
    if (_discarded) {
      // The wipe landed while we were creating the directory. Take the empty
      // tree back out: a write must never leave the store's shape behind.
      await _removeDirectoryIfEmpty();
      throw const StoreDiscarded('wiped while an evidence file was in flight');
    }
    final target = fileNamed(name);
    final temporary = File('${target.path}.tmp');
    await temporary.writeAsBytes(
      cipher.seal(plaintext, context: contextFor(purpose, reference)),
      flush: true,
    );
    if (_discarded) {
      await _removeQuietly(temporary);
      await _removeDirectoryIfEmpty();
      throw const StoreDiscarded('wiped while an evidence file was in flight');
    }
    await temporary.rename(target.path);
    return target;
  });

  Future<Uint8List> read(
    File file, {
    required String purpose,
    required String reference,
  }) => work.run(() async {
    _refuse('read $purpose');
    final envelope = await file.readAsBytes();
    return cipher.open(envelope, context: contextFor(purpose, reference));
  });

  Future<void> delete(File file) => work.run(() async {
    if (await file.exists()) {
      try {
        await file.delete();
      } on FileSystemException {
        // Cleanup is best effort; the row that pointed here is already gone.
      }
    }
  });

  void _refuse(String action) {
    if (_discarded) throw StoreDiscarded('$action after wipe');
  }

  Future<void> _removeQuietly(File file) async {
    try {
      if (await file.exists()) await file.delete();
    } on FileSystemException {
      // Best effort: the bytes are an unopenable envelope either way.
    }
  }

  /// Removes the evidence directory, and the scope root above it, when a
  /// refused write is all that recreated them.
  Future<void> _removeDirectoryIfEmpty() async {
    for (final candidate in [directory, directory.parent]) {
      try {
        if (await candidate.exists() && await candidate.list().isEmpty) {
          await candidate.delete();
        }
      } on FileSystemException {
        // Best effort: an empty directory is not readable residue.
      }
    }
  }
}

import 'dart:io';

import 'package:drift/drift.dart';
import 'package:image/image.dart' as img;
import 'package:image_picker/image_picker.dart';
import 'package:uuid/uuid.dart';

import '../location/location_source.dart';
import '../offline/database.dart';

/// Source of raw photo bytes. The device implementation uses image_picker's
/// camera; tests inject canned bytes.
abstract class ImageSourceAdapter {
  Future<Uint8List?> pick();

  /// Recovers a camera result when Android recreated the activity/process
  /// while the external camera application was in the foreground.
  Future<Uint8List?> recoverLost();
}

class CameraImageSource implements ImageSourceAdapter {
  final _picker = ImagePicker();

  @override
  Future<Uint8List?> pick() async {
    final file = await _picker.pickImage(
      source: ImageSource.camera,
      imageQuality: 90,
      maxWidth: cameraCaptureMaxWidth,
      maxHeight: cameraCaptureMaxHeight,
    );
    return file?.readAsBytes();
  }

  @override
  Future<Uint8List?> recoverLost() async {
    final response = await _picker.retrieveLostData();
    if (response.isEmpty || response.files == null || response.files!.isEmpty) {
      return null;
    }
    return response.files!.first.readAsBytes();
  }
}

class FakeImageSource implements ImageSourceAdapter {
  FakeImageSource(this.bytes, {this.lostBytes});

  Uint8List? bytes;
  Uint8List? lostBytes;

  @override
  Future<Uint8List?> pick() async => bytes;

  @override
  Future<Uint8List?> recoverLost() async {
    final recovered = lostBytes;
    lostBytes = null;
    return recovered;
  }
}

const maxPhotoDimension = 1600;
const jpegQuality = 85;

/// Native capture bounds keep the camera result small enough to cross the
/// Android activity boundary without forcing the field app out of memory.
const cameraCaptureMaxWidth = 1600.0;
const cameraCaptureMaxHeight = 1600.0;

/// Downscale to [maxPhotoDimension] on the long edge and re-encode as JPEG.
/// Undecodable bytes pass through unchanged (the server validates MIME).
Uint8List processPhoto(Uint8List raw) {
  final decoded = img.decodeImage(raw);
  if (decoded == null) return raw;
  var image = decoded;
  final longEdge = image.width > image.height ? image.width : image.height;
  if (longEdge > maxPhotoDimension) {
    image = image.width >= image.height
        ? img.copyResize(image, width: maxPhotoDimension)
        : img.copyResize(image, height: maxPhotoDimension);
  }
  return Uint8List.fromList(img.encodeJpg(image, quality: jpegQuality));
}

/// Captures photos into the offline queue: processed file on disk + a
/// PendingPhotos row that the sync service uploads.
class PhotoQueue {
  PhotoQueue({
    required this.db,
    required this.source,
    required this.location,
    required this.storageDir,
  });

  final AppDatabase db;
  final ImageSourceAdapter source;
  final LocationSource location;
  final Directory storageDir;

  static const _uuid = Uuid();

  File get _pendingCompletionCapture =>
      File('${storageDir.path}/.pending_completion_capture');

  Future<bool> captureForJob({
    String? workOrderId,
    String? installationProjectId,
    String kind = 'photo',
  }) async {
    if (workOrderId != null && kind == 'photo') {
      await _pendingCompletionCapture.writeAsString(workOrderId, flush: true);
    }
    try {
      final raw = await source.pick();
      if (raw == null) return false;
      final position = await location.current();
      await enqueueImageBytes(
        raw,
        kind: kind,
        workOrderId: workOrderId,
        installationProjectId: installationProjectId,
        latitude: position?.latitude,
        longitude: position?.longitude,
      );
      return true;
    } finally {
      await _deletePendingCompletionCapture();
    }
  }

  Future<bool> recoverForJob({required String workOrderId}) async {
    if (!await _pendingCompletionCapture.exists() ||
        await _pendingCompletionCapture.readAsString() != workOrderId) {
      return false;
    }
    try {
      final raw = await source.recoverLost();
      if (raw == null) return false;
      final position = await location.current();
      await enqueueImageBytes(
        raw,
        kind: 'photo',
        workOrderId: workOrderId,
        latitude: position?.latitude,
        longitude: position?.longitude,
      );
      return true;
    } finally {
      await _deletePendingCompletionCapture();
    }
  }

  Future<void> _deletePendingCompletionCapture() async {
    try {
      if (await _pendingCompletionCapture.exists()) {
        await _pendingCompletionCapture.delete();
      }
    } on FileSystemException {
      // Best effort: a stale marker cannot recover without image-picker data.
    }
  }

  /// Queue already-captured image bytes (e.g. a rendered signature) for upload.
  /// Bytes run through [processPhoto] so the stored file is JPEG, matching the
  /// upload's photo.jpg filename / image-jpeg content type.
  Future<void> enqueueImageBytes(
    Uint8List bytes, {
    required String kind,
    String? workOrderId,
    String? installationProjectId,
    double? latitude,
    double? longitude,
  }) async {
    final processed = processPhoto(bytes);
    final clientRef = _uuid.v4();
    final file = File('${storageDir.path}/$clientRef.jpg');
    await file.writeAsBytes(processed, flush: true);
    try {
      await db
          .into(db.pendingPhotos)
          .insert(
            PendingPhotosCompanion.insert(
              clientRef: clientRef,
              localPath: file.path,
              kind: Value(kind),
              workOrderId: Value(workOrderId),
              installationProjectId: Value(installationProjectId),
              latitude: Value(latitude),
              longitude: Value(longitude),
              capturedAt: DateTime.now().toUtc(),
            ),
          );
    } catch (_) {
      // Don't orphan the file if the row insert fails.
      try {
        file.deleteSync();
      } on FileSystemException {
        // best effort
      }
      rethrow;
    }
  }

  Future<int> pendingCount() async {
    final rows = await (db.select(
      db.pendingPhotos,
    )..where((row) => row.uploaded.equals(false))).get();
    return rows.length;
  }
}

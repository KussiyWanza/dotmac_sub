import 'dart:typed_data';

/// An authenticated PDF returned by the billing API.
class BillingDocument {
  BillingDocument({required this.bytes, required this.filename});

  final Uint8List bytes;
  final String filename;
  static const contentType = 'application/pdf';
}

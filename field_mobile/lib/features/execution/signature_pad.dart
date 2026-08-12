import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

/// Finger-drawn signature canvas; exports PNG bytes for upload.
class SignaturePadController {
  final strokes = <List<Offset>>[];

  bool get hasInk => strokes.any((stroke) => stroke.length > 1);

  void clear() => strokes.clear();

  Future<Uint8List> toPng(Size size) async {
    final recorder = ui.PictureRecorder();
    final canvas = Canvas(recorder);
    canvas.drawRect(
      Offset.zero & size,
      Paint()..color = const Color(0xFFFFFFFF),
    );
    final paint = Paint()
      ..color = const Color(0xFF0F172A)
      ..strokeWidth = 3
      ..strokeCap = StrokeCap.round;
    for (final stroke in strokes) {
      for (var i = 0; i + 1 < stroke.length; i++) {
        canvas.drawLine(stroke[i], stroke[i + 1], paint);
      }
    }
    final image = await recorder.endRecording().toImage(
      size.width.toInt(),
      size.height.toInt(),
    );
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    return bytes!.buffer.asUint8List();
  }
}

class SignaturePad extends StatefulWidget {
  const SignaturePad({super.key, required this.controller, this.onChanged});

  final SignaturePadController controller;
  final VoidCallback? onChanged;

  @override
  State<SignaturePad> createState() => _SignaturePadState();
}

class _SignaturePadState extends State<SignaturePad> {
  @override
  Widget build(BuildContext context) {
    return Container(
      height: 220,
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Theme.of(context).colorScheme.outlineVariant),
      ),
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onPanStart: (details) => setState(() {
          widget.controller.strokes.add([details.localPosition]);
        }),
        onPanUpdate: (details) => setState(() {
          widget.controller.strokes.last.add(details.localPosition);
          widget.onChanged?.call();
        }),
        child: CustomPaint(
          painter: _SignaturePainter(widget.controller.strokes),
          size: Size.infinite,
        ),
      ),
    );
  }
}

class _SignaturePainter extends CustomPainter {
  _SignaturePainter(this.strokes);

  final List<List<Offset>> strokes;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = const Color(0xFF0F172A)
      ..strokeWidth = 3
      ..strokeCap = StrokeCap.round;
    for (final stroke in strokes) {
      for (var i = 0; i + 1 < stroke.length; i++) {
        canvas.drawLine(stroke[i], stroke[i + 1], paint);
      }
    }
  }

  @override
  bool shouldRepaint(_SignaturePainter oldDelegate) => true;
}

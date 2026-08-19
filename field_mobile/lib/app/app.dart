import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/push/push_registrar.dart';
import 'router.dart';
import 'theme.dart';

class DotmacFieldApp extends ConsumerStatefulWidget {
  const DotmacFieldApp({super.key});

  @override
  ConsumerState<DotmacFieldApp> createState() => _DotmacFieldAppState();
}

class _DotmacFieldAppState extends ConsumerState<DotmacFieldApp> {
  PushRegistrar? _registrar;

  @override
  void initState() {
    super.initState();
    Future.microtask(() {
      if (!mounted) return;
      final registrar = ref.read(pushRegistrarProvider);
      registrar.start();
      _registrar = registrar;
    });
  }

  @override
  void dispose() {
    _registrar?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp.router(
      restorationScopeId: 'dotmac-field-app',
      title: 'DotMac Field',
      debugShowCheckedModeBanner: false,
      scaffoldMessengerKey: pushScaffoldMessengerKey,
      theme: lightTheme,
      darkTheme: darkTheme,
      routerConfig: ref.watch(routerProvider),
    );
  }
}

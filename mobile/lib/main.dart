import 'dart:async';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:sentry/sentry.dart';

import 'src/app.dart';
import 'src/config/env.dart';
import 'src/core/push_service.dart';

/// Register the FCM background-message handler before the app starts. Guarded:
/// without the platform config (google-services.json / GoogleService-Info.plist)
/// Firebase init fails and push is simply disabled — the app runs normally.
Future<void> _initPushBackground() async {
  try {
    if (Firebase.apps.isEmpty) {
      await Firebase.initializeApp();
    }
    FirebaseMessaging.onBackgroundMessage(firebaseMessagingBackgroundHandler);
  } catch (_) {
    // Push disabled for this build (no Firebase config); in-app inbox still works.
  }
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  if (kDebugMode) {
    debugPrint('[config] API_ROOT=${Env.apiRoot}');
  }
  await _initPushBackground();

  const app = ProviderScope(child: DotMacApp());

  // Crash reporting is opt-in and goes to your self-hosted GlitchTip
  // (Sentry-protocol). With no GLITCHTIP_DSN supplied at build time the app
  // runs exactly as before and nothing is initialised or sent.
  if (Env.glitchtipDsn.isEmpty) {
    runApp(app);
    return;
  }

  runZonedGuarded(
    () async {
      await Sentry.init((options) {
        options.dsn = Env.glitchtipDsn;
        options.environment = Env.glitchtipEnvironment;
        // Identifies app events in the shared GlitchTip project (filter by
        // release:dotmac-mobile@* or environment:mobile-*).
        options.release = 'dotmac-mobile@7.144.4';
        options.sendDefaultPii = false;
      });

      // Forward framework + platform errors into GlitchTip.
      FlutterError.onError = (details) {
        FlutterError.presentError(details);
        Sentry.captureException(details.exception, stackTrace: details.stack);
      };
      PlatformDispatcher.instance.onError = (error, stack) {
        Sentry.captureException(error, stackTrace: stack);
        return true;
      };

      runApp(app);
    },
    (error, stack) => Sentry.captureException(error, stackTrace: stack),
  );
}

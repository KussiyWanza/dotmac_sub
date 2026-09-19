import 'dart:convert';

import 'package:dio/dio.dart';

import 'token_store.dart';

/// Decode a JWT's exp claim without verifying the signature (the server
/// verifies; the client only needs the expiry for proactive refresh).
DateTime? jwtExpiry(String token) {
  final claims = jwtClaims(token);
  final exp = claims?['exp'];
  if (exp is! int) return null;
  return DateTime.fromMillisecondsSinceEpoch(exp * 1000, isUtc: true);
}

/// Stable authenticated subject used only to isolate local projection caches.
/// Authorization remains server-owned; this never validates or trusts the JWT.
String? jwtSubject(String token) {
  final subject = jwtClaims(token)?['sub'];
  if (subject is! String || subject.trim().isEmpty) return null;
  return subject.trim();
}

/// Decodes a JWT's claims without verifying the signature. The server verifies;
/// the client reads them only to decide which local storage scope the session
/// belongs to, never to decide what the session may do.
Map<String, dynamic>? jwtClaims(String token) {
  final parts = token.split('.');
  if (parts.length != 3) return null;
  try {
    var payload = parts[1].replaceAll('-', '+').replaceAll('_', '/');
    while (payload.length % 4 != 0) {
      payload += '=';
    }
    final claims = jsonDecode(utf8.decode(base64.decode(payload)));
    if (claims is! Map) return null;
    return claims.cast<String, dynamic>();
  } on Object {
    return null;
  }
}

/// What the client learned when it tried to make the stored session usable.
///
/// This type exists for one distinction, ADR-0067 § 4: a timeout, a DNS
/// failure, a TLS failure and a 5xx are transport facts, and none of them ends
/// a session or destroys a credential. Only an authoritative refusal on the
/// refresh exchange itself does. Collapsing both into a bare `null` is what
/// made a coverage hole indistinguishable from a revocation at cold start.
sealed class SessionRefresh {
  const SessionRefresh();
}

/// A usable access token: either the stored one, still current, or a rotated
/// one the server has just issued.
final class SessionFresh extends SessionRefresh {
  const SessionFresh(this.accessToken);

  final String accessToken;
}

/// Nothing is stored that could authenticate anyone. Not a failure — either
/// nobody is signed in, or the session ended while this exchange was in
/// flight and its result is no longer anybody's.
final class SessionAbsent extends SessionRefresh {
  const SessionAbsent();
}

/// The exchange never reached an authoritative answer. The stored refresh
/// credential is untouched and a later attempt may well succeed; the device is
/// offline, not signed out.
final class SessionUnreachable extends SessionRefresh {
  const SessionUnreachable();
}

/// The server refused the refresh exchange itself (401/403), or the stored
/// credential is provably unusable without asking anyone — no refresh token to
/// exchange, or a success response carrying no access token. Terminal.
final class SessionRefused extends SessionRefresh {
  const SessionRefused();
}

/// Dio wrapper that injects the bearer token, proactively refreshes it
/// shortly before expiry, and retries once on 401 after a refresh.
class ApiClient {
  static const nativeRefreshHeader = 'X-Auth-Refresh-In-Body';

  ApiClient({
    required this.baseUrl,
    required this.tokenStore,
    Dio? dio,
    Dio? refreshDio,
    this.onSessionExpired,
  }) : dio = dio ?? Dio(),
       // Separate transport for token refresh so it never recurses through
       // our own auth interceptor; injectable for tests.
       _refreshDio = refreshDio ?? Dio(BaseOptions(baseUrl: baseUrl)) {
    this.dio.options.baseUrl = baseUrl;
    this.dio.options.connectTimeout = const Duration(seconds: 10);
    this.dio.options.receiveTimeout = const Duration(seconds: 20);
    this.dio.options.headers[nativeRefreshHeader] = 'true';
    _refreshDio.options.headers[nativeRefreshHeader] = 'true';
    this.dio.interceptors.add(_AuthInterceptor(this));
  }

  final String baseUrl;
  final TokenStore tokenStore;
  final Dio dio;
  final Dio _refreshDio;

  /// Called when refresh fails: the UI logs the user out.
  final void Function()? onSessionExpired;

  static const _refreshSkew = Duration(seconds: 60);

  // A single shared in-flight refresh: concurrent callers await the SAME
  // future and all receive the freshly-saved token, instead of one rotating
  // the refresh token while others replay the stale one (which trips the
  // server's reuse-detection and forces a spurious logout).
  Future<SessionRefresh>? _inFlight;

  // Bumped by [abandonSession]. A refresh captures it on entry and refuses to
  // persist its result if it has moved by the time the server answers.
  int _generation = 0;

  /// The session owner is tearing this session down. A refresh already in
  /// flight may still succeed at the server, and its rotated credential must
  /// not be written back onto a device the wipe has just cleared. Nothing is
  /// cancelled — the exchange is simply no longer anybody's.
  void abandonSession() {
    _generation++;
    _inFlight = null;
  }

  /// The typed answer. Callers that need to tell "offline" from "refused" —
  /// above all the cold-start restore — use this rather than the token-shaped
  /// views below, whose `null` cannot carry the difference.
  Future<SessionRefresh> ensureFreshSession() async {
    final access = await tokenStore.accessToken;
    if (access == null) {
      // No access token, but a refresh credential can still mint one; only the
      // absence of both means there is no session to restore.
      final refreshToken = await tokenStore.refreshToken;
      if (refreshToken == null) return const SessionAbsent();
      return refreshSession();
    }
    final expiry = jwtExpiry(access);
    if (expiry == null ||
        expiry.isAfter(DateTime.now().toUtc().add(_refreshSkew))) {
      return SessionFresh(access);
    }
    return refreshSession();
  }

  Future<SessionRefresh> refreshSession() {
    return _inFlight ??= _doRefresh().whenComplete(() => _inFlight = null);
  }

  /// Token-shaped view for callers that only need the bearer value — the
  /// request interceptor. A `null` here means "no token to send"; it has never
  /// meant, and must not be read as, "the session is over".
  Future<String?> ensureFreshToken() async =>
      _bearerOf(await ensureFreshSession());

  Future<String?> refresh() async => _bearerOf(await refreshSession());

  String? _bearerOf(SessionRefresh outcome) =>
      outcome is SessionFresh ? outcome.accessToken : null;

  Future<SessionRefresh> _doRefresh() async {
    final generation = _generation;
    try {
      final refreshToken = await tokenStore.refreshToken;
      if (refreshToken == null) {
        // Decided locally, with no network involved: there is nothing left on
        // the device that could ever mint an access token.
        onSessionExpired?.call();
        return const SessionRefused();
      }
      final mode = await tokenStore.loginMode ?? LoginMode.staff;
      final path = mode == LoginMode.vendor
          ? '/api/v1/vendor/auth/refresh'
          : '/api/v1/auth/refresh';
      final response = await _refreshDio.post(
        path,
        data: {'refresh_token': refreshToken},
      );
      final data = response.data as Map;
      final access = data['access_token'] as String?;
      if (access == null) {
        onSessionExpired?.call();
        return const SessionRefused();
      }
      if (generation != _generation) {
        // The session ended while this exchange was in flight. Saving the
        // rotated credential now would put a live token back on a handset that
        // has just been wiped, so it is dropped instead.
        return const SessionAbsent();
      }
      await tokenStore.save(
        accessToken: access,
        refreshToken: data['refresh_token'] as String?,
      );
      return SessionFresh(access);
    } on DioException catch (error) {
      // Only an authoritative authentication refusal ends the local session.
      // Timeouts, offline periods, and server failures must leave the rotating
      // refresh token intact so a later request can retry after recovery.
      final status = error.response?.statusCode;
      if (status == 401 || status == 403) {
        onSessionExpired?.call();
        return const SessionRefused();
      }
      return const SessionUnreachable();
    } on Object {
      // A body that would not parse, a secure store that would not read: none
      // of it is the server refusing the session, so none of it ends one.
      return const SessionUnreachable();
    }
  }
}

class _AuthInterceptor extends Interceptor {
  _AuthInterceptor(this.client);

  final ApiClient client;

  static const _public = [
    '/auth/login',
    '/auth/mfa',
    '/auth/refresh',
    '/auth/forgot-password',
    '/field/config',
  ];

  bool _isPublic(String path) => _public.any(path.contains);

  @override
  Future<void> onRequest(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    if (!_isPublic(options.path)) {
      final token = await client.ensureFreshToken();
      if (token != null) {
        options.headers['Authorization'] = 'Bearer $token';
      }
    }
    handler.next(options);
  }

  @override
  Future<void> onError(
    DioException err,
    ErrorInterceptorHandler handler,
  ) async {
    final response = err.response;
    final alreadyRetried = err.requestOptions.extra['retried'] == true;
    if (response?.statusCode == 401 &&
        !alreadyRetried &&
        !_isPublic(err.requestOptions.path)) {
      // A multipart body is a one-shot stream — already consumed by the failed
      // attempt, so we can't refetch it. The photo/attachment outbox retries
      // these uploads itself, so don't auto-retry them here.
      if (err.requestOptions.data is FormData) {
        handler.next(err);
        return;
      }
      final token = await client.refresh();
      if (token != null) {
        final options = err.requestOptions..extra['retried'] = true;
        options.headers['Authorization'] = 'Bearer $token';
        try {
          final retried = await client.dio.fetch(options);
          return handler.resolve(retried);
        } on DioException catch (retryError) {
          return handler.next(retryError);
        }
      }
    }
    handler.next(err);
  }
}

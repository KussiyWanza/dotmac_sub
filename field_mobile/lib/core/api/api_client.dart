import 'dart:convert';

import 'package:dio/dio.dart';

import 'token_store.dart';

/// Decode a JWT's exp claim without verifying the signature (the server
/// verifies; the client only needs the expiry for proactive refresh).
DateTime? jwtExpiry(String token) {
  final claims = _jwtClaims(token);
  final exp = claims?['exp'];
  if (exp is! int) return null;
  return DateTime.fromMillisecondsSinceEpoch(exp * 1000, isUtc: true);
}

/// Stable authenticated subject used only to isolate local projection caches.
/// Authorization remains server-owned; this never validates or trusts the JWT.
String? jwtSubject(String token) {
  final subject = _jwtClaims(token)?['sub'];
  if (subject is! String || subject.trim().isEmpty) return null;
  return subject.trim();
}

Map<String, dynamic>? _jwtClaims(String token) {
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
  Future<String?>? _inFlight;

  Future<String?> ensureFreshToken() async {
    final access = await tokenStore.accessToken;
    if (access == null) return null;
    final expiry = jwtExpiry(access);
    if (expiry == null ||
        expiry.isAfter(DateTime.now().toUtc().add(_refreshSkew))) {
      return access;
    }
    return refresh();
  }

  Future<String?> refresh() {
    return _inFlight ??= _doRefresh().whenComplete(() => _inFlight = null);
  }

  Future<String?> _doRefresh() async {
    try {
      final refreshToken = await tokenStore.refreshToken;
      if (refreshToken == null) {
        onSessionExpired?.call();
        return null;
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
        return null;
      }
      await tokenStore.save(
        accessToken: access,
        refreshToken: data['refresh_token'] as String?,
      );
      return access;
    } on DioException catch (error) {
      // Only an authoritative authentication refusal ends the local session.
      // Timeouts, offline periods, and server failures must leave the rotating
      // refresh token intact so a later request can retry after recovery.
      final status = error.response?.statusCode;
      if (status == 401 || status == 403) {
        onSessionExpired?.call();
      }
      return null;
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

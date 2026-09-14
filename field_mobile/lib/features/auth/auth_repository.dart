import 'package:dio/dio.dart';

import '../../core/api/api_client.dart';
import '../../core/api/token_store.dart';

/// Build version, injected at compile time via `--dart-define=APP_VERSION=x.y.z`
/// (CI passes the pubspec version, e.g. `flutter build --dart-define=APP_VERSION=$VERSION`).
/// Falls back to the pubspec baseline for local/dev builds. Compared against
/// /field/config min_app_version for upgrade gating.
const appVersion = String.fromEnvironment('APP_VERSION', defaultValue: '1.0.0');

sealed class LoginResult {
  const LoginResult();
}

class LoginSuccess extends LoginResult {
  const LoginSuccess(this.mode, {this.vendorId});

  final LoginMode mode;
  final String? vendorId;
}

class MfaRequired extends LoginResult {
  const MfaRequired(this.mfaToken, this.mode);

  final String mfaToken;
  final LoginMode mode;
}

class LoginFailure extends LoginResult {
  const LoginFailure(this.message);

  final String message;
}

/// Typed mobile request for the canonical credential-recovery owner.
final class PasswordRecoveryRequest {
  const PasswordRecoveryRequest({required this.email});

  final String email;
}

sealed class PasswordRecoveryResult {
  const PasswordRecoveryResult();
}

final class PasswordRecoveryAccepted extends PasswordRecoveryResult {
  const PasswordRecoveryAccepted(this.message);

  final String message;
}

final class PasswordRecoveryFailure extends PasswordRecoveryResult {
  const PasswordRecoveryFailure(this.message);

  final String message;
}

class AppConfig {
  const AppConfig({
    required this.minAppVersion,
    required this.latestAppVersion,
    required this.featureFlags,
  });

  final String minAppVersion;
  final String latestAppVersion;
  final Map<String, bool> featureFlags;

  bool get upgradeRequired => compareSemver(appVersion, minAppVersion) < 0;
}

/// Compare dotted versions: negative when [a] < [b].
int compareSemver(String a, String b) {
  List<int> parse(String v) => v
      .split('.')
      .map(
        (part) => int.tryParse(part.replaceAll(RegExp(r'[^0-9].*$'), '')) ?? 0,
      )
      .toList();
  final left = parse(a);
  final right = parse(b);
  for (var i = 0; i < 3; i++) {
    final l = i < left.length ? left[i] : 0;
    final r = i < right.length ? right[i] : 0;
    if (l != r) return l.compareTo(r);
  }
  return 0;
}

class AuthRepository {
  AuthRepository(this.client);

  final ApiClient client;

  TokenStore get _store => client.tokenStore;

  Future<AppConfig> fetchConfig() async {
    final response = await client.dio.get('/api/v1/field/config');
    final data = response.data as Map;
    final flags = (data['feature_flags'] as Map?) ?? {};
    return AppConfig(
      minAppVersion: data['min_app_version'] as String? ?? '0.0.0',
      latestAppVersion: data['latest_app_version'] as String? ?? '0.0.0',
      featureFlags: flags.map((k, v) => MapEntry(k.toString(), v == true)),
    );
  }

  Future<LoginResult> login({
    required String username,
    required String password,
    required LoginMode mode,
  }) async {
    final path = mode == LoginMode.vendor
        ? '/api/v1/vendor/auth/login'
        : '/api/v1/auth/login';
    try {
      final response = await client.dio.post(
        path,
        data: {'username': username, 'password': password},
      );
      return await _handleTokens(response.data as Map, mode);
    } on DioException catch (error) {
      return LoginFailure(_message(error));
    }
  }

  Future<LoginResult> verifyMfa({
    required String mfaToken,
    required String code,
    required LoginMode mode,
  }) async {
    final path = mode == LoginMode.vendor
        ? '/api/v1/vendor/auth/mfa'
        : '/api/v1/auth/mfa/verify';
    try {
      final response = await client.dio.post(
        path,
        data: {'mfa_token': mfaToken, 'code': code},
      );
      return await _handleTokens(response.data as Map, mode);
    } on DioException catch (error) {
      return LoginFailure(_message(error));
    }
  }

  Future<PasswordRecoveryResult> requestPasswordRecovery(
    PasswordRecoveryRequest request,
  ) async {
    try {
      final response = await client.dio.post(
        '/api/v1/auth/forgot-password',
        data: {'email': request.email},
      );
      final data = response.data as Map;
      return PasswordRecoveryAccepted(
        data['message'] as String? ??
            'If the email exists, a reset link has been sent',
      );
    } on DioException catch (error) {
      return PasswordRecoveryFailure(_message(error));
    }
  }

  Future<void> logout() => _store.clear();

  Future<LoginResult> _handleTokens(Map data, LoginMode mode) async {
    if (data['mfa_required'] == true) {
      return MfaRequired(data['mfa_token'] as String, mode);
    }
    final access = data['access_token'] as String?;
    if (access == null) {
      return const LoginFailure('Unexpected server response');
    }
    await _store.save(
      accessToken: access,
      refreshToken: data['refresh_token'] as String?,
      loginMode: mode,
    );
    return LoginSuccess(mode, vendorId: data['vendor_id'] as String?);
  }

  String _message(DioException error) {
    final data = error.response?.data;
    if (data is Map && data['detail'] is String) {
      return data['detail'] as String;
    }
    if (error.response?.statusCode == 401) return 'Invalid credentials';
    if (error.response?.statusCode == 403) return 'Access denied';
    return 'Connection problem — try again';
  }
}

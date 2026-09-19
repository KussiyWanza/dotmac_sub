import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/features/auth/auth_repository.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fake_http.dart';

ApiClient _client(
  FakeHttpAdapter adapter,
  TokenStore store, {
  void Function()? onExpired,
}) {
  final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
  dio.httpClientAdapter = adapter;
  final refreshDio = Dio(BaseOptions(baseUrl: 'https://test.local'));
  refreshDio.httpClientAdapter = adapter;
  return ApiClient(
    baseUrl: 'https://test.local',
    tokenStore: store,
    dio: dio,
    refreshDio: refreshDio,
    onSessionExpired: onExpired,
  );
}

/// A token store whose persistence step fails with the one exception class
/// the repository's error path knows how to convert. `_handleTokens` awaits
/// `save`, so the throw only lands inside `login`/`verifyMfa`'s `try` frame
/// when those methods await `_handleTokens` in turn.
class _FailingSaveTokenStore extends InMemoryTokenStore {
  @override
  Future<void> save({
    required String accessToken,
    String? refreshToken,
    LoginMode? loginMode,
  }) async {
    throw DioException(
      requestOptions: RequestOptions(path: '/token-store/save'),
      message: 'token storage unavailable',
    );
  }
}

void main() {
  late FakeHttpAdapter adapter;
  late InMemoryTokenStore store;
  late AuthRepository repo;

  setUp(() {
    adapter = FakeHttpAdapter();
    store = InMemoryTokenStore();
    repo = AuthRepository(_client(adapter, store));
  });

  final freshToken = fakeJwt(
    expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
  );

  test('staff login stores tokens and mode', () async {
    adapter.on('POST', '/api/v1/auth/login', (options) {
      expect(options.headers[ApiClient.nativeRefreshHeader], 'true');
      return (200, {'access_token': freshToken, 'refresh_token': 'refresh-1'});
    });

    final result = await repo.login(
      username: 'tech@dotmac.io',
      password: 'pw',
      mode: LoginMode.staff,
    );

    expect(result, isA<LoginSuccess>());
    expect(await store.accessToken, freshToken);
    expect(await store.refreshToken, 'refresh-1');
    expect(await store.loginMode, LoginMode.staff);
  });

  test('vendor login uses vendor path and captures vendor context', () async {
    adapter.on(
      'POST',
      '/api/v1/vendor/auth/login',
      (_) => (
        200,
        {
          'access_token': freshToken,
          'refresh_token': 'refresh-v',
          'vendor_id': 'vendor-9',
        },
      ),
    );

    final result = await repo.login(
      username: 'crew@vendor.io',
      password: 'pw',
      mode: LoginMode.vendor,
    );

    expect(result, isA<LoginSuccess>());
    expect((result as LoginSuccess).vendorId, 'vendor-9');
    expect(await store.loginMode, LoginMode.vendor);
  });

  test(
    'auth controller restores a persisted staff session on cold start',
    () async {
      await store.save(
        accessToken: freshToken,
        refreshToken: 'refresh-1',
        loginMode: LoginMode.staff,
      );
      final client = _client(adapter, store);
      final container = ProviderContainer(
        overrides: [
          tokenStoreProvider.overrideWithValue(store),
          apiClientProvider.overrideWithValue(client),
        ],
      );
      addTearDown(container.dispose);

      expect(container.read(authControllerProvider), isA<RestoringSession>());
      await Future<void>.delayed(Duration.zero);
      await Future<void>.delayed(Duration.zero);

      final state = container.read(authControllerProvider);
      expect(state, isA<Authenticated>());
      expect((state as Authenticated).mode, LoginMode.staff);
    },
  );

  test(
    'auth controller restores a persisted vendor session on cold start',
    () async {
      await store.save(
        accessToken: freshToken,
        refreshToken: 'refresh-v',
        loginMode: LoginMode.vendor,
      );
      final client = _client(adapter, store);
      final container = ProviderContainer(
        overrides: [
          tokenStoreProvider.overrideWithValue(store),
          apiClientProvider.overrideWithValue(client),
        ],
      );
      addTearDown(container.dispose);

      expect(container.read(authControllerProvider), isA<RestoringSession>());
      await Future<void>.delayed(Duration.zero);
      await Future<void>.delayed(Duration.zero);

      final state = container.read(authControllerProvider);
      expect(state, isA<Authenticated>());
      expect((state as Authenticated).mode, LoginMode.vendor);
    },
  );

  test('mfa challenge then verify', () async {
    adapter.on(
      'POST',
      '/api/v1/auth/login',
      (_) => (200, {'mfa_required': true, 'mfa_token': 'mfa-1'}),
    );
    adapter.on(
      'POST',
      '/api/v1/auth/mfa/verify',
      (_) => (200, {'access_token': freshToken, 'refresh_token': 'refresh-2'}),
    );

    final challenge = await repo.login(
      username: 'tech@dotmac.io',
      password: 'pw',
      mode: LoginMode.staff,
    );
    expect(challenge, isA<MfaRequired>());

    final verified = await repo.verifyMfa(
      mfaToken: (challenge as MfaRequired).mfaToken,
      code: '123456',
      mode: LoginMode.staff,
    );
    expect(verified, isA<LoginSuccess>());
    expect(await store.refreshToken, 'refresh-2');
  });

  test('bad credentials surface server detail', () async {
    adapter.on(
      'POST',
      '/api/v1/auth/login',
      (_) => (401, {'detail': 'Invalid credentials'}),
    );
    final result = await repo.login(
      username: 'x',
      password: 'y',
      mode: LoginMode.staff,
    );
    expect(result, isA<LoginFailure>());
    expect((result as LoginFailure).message, 'Invalid credentials');
  });

  test('password recovery submits a typed email request', () async {
    adapter.on('POST', '/api/v1/auth/forgot-password', (options) {
      expect(options.data, {'email': 'tech@dotmac.io'});
      expect(options.headers['Authorization'], isNull);
      return (
        200,
        {'message': 'If the email exists, a reset link has been sent'},
      );
    });

    final result = await repo.requestPasswordRecovery(
      const PasswordRecoveryRequest(email: 'tech@dotmac.io'),
    );

    expect(result, isA<PasswordRecoveryAccepted>());
    expect(
      (result as PasswordRecoveryAccepted).message,
      'If the email exists, a reset link has been sent',
    );
  });

  test('password recovery returns a typed transport failure', () async {
    adapter.on(
      'POST',
      '/api/v1/auth/forgot-password',
      (_) => (503, {'detail': 'Recovery is temporarily unavailable'}),
    );

    final result = await repo.requestPasswordRecovery(
      const PasswordRecoveryRequest(email: 'tech@dotmac.io'),
    );

    expect(result, isA<PasswordRecoveryFailure>());
    expect(
      (result as PasswordRecoveryFailure).message,
      'Recovery is temporarily unavailable',
    );
  });

  // Regression (ported from dotmac_crm 50beb0cb): `return _handleTokens(...)`
  // without `await` hands the future back after the `try` frame has already
  // been left, so a failure while persisting tokens escapes `on DioException`
  // instead of coming back through the repository's normal failure path.
  test('login reports a token-persistence failure as a LoginFailure', () async {
    adapter.on(
      'POST',
      '/api/v1/auth/login',
      (_) => (200, {'access_token': freshToken, 'refresh_token': 'refresh-1'}),
    );
    final failing = AuthRepository(_client(adapter, _FailingSaveTokenStore()));

    final result = await failing.login(
      username: 'tech@dotmac.io',
      password: 'pw',
      mode: LoginMode.staff,
    );

    expect(result, isA<LoginFailure>());
  });

  test(
    'mfa verification reports a token-persistence failure as a LoginFailure',
    () async {
      adapter.on(
        'POST',
        '/api/v1/auth/mfa/verify',
        (_) =>
            (200, {'access_token': freshToken, 'refresh_token': 'refresh-2'}),
      );
      final failing = AuthRepository(
        _client(adapter, _FailingSaveTokenStore()),
      );

      final result = await failing.verifyMfa(
        mfaToken: 'mfa-1',
        code: '123456',
        mode: LoginMode.staff,
      );

      expect(result, isA<LoginFailure>());
    },
  );

  test('401 triggers refresh and retries once with the new token', () async {
    // Token looks healthy (proactive refresh skips it) but the server has
    // revoked it — the 401 path must refresh and retry.
    final revoked = fakeJwt(
      expiry: DateTime.now().toUtc().add(const Duration(minutes: 10)),
    );
    await store.save(
      accessToken: revoked,
      refreshToken: 'refresh-old',
      loginMode: LoginMode.staff,
    );

    var jobCalls = 0;
    adapter.on('GET', '/api/v1/field/jobs', (options) {
      jobCalls++;
      final auth = options.headers['Authorization'] as String?;
      if (auth == 'Bearer $freshToken') return (200, {'items': [], 'count': 0});
      return (401, {'detail': 'Unauthorized'});
    });
    adapter.on(
      'POST',
      '/api/v1/auth/refresh',
      (_) =>
          (200, {'access_token': freshToken, 'refresh_token': 'refresh-new'}),
    );

    final client = _client(adapter, store);
    final response = await client.dio.get('/api/v1/field/jobs');

    expect(response.statusCode, 200);
    expect(jobCalls, 2); // 401 then retried with the refreshed token
    expect(await store.accessToken, freshToken);
    expect(await store.refreshToken, 'refresh-new');
  });

  test('proactive refresh fires before expiry without a 401', () async {
    final expiringSoon = fakeJwt(
      expiry: DateTime.now().toUtc().add(const Duration(seconds: 30)),
    );
    await store.save(
      accessToken: expiringSoon,
      refreshToken: 'refresh-old',
      loginMode: LoginMode.staff,
    );

    adapter.on('POST', '/api/v1/auth/refresh', (options) {
      expect(options.headers[ApiClient.nativeRefreshHeader], 'true');
      return (200, {'access_token': freshToken});
    });
    adapter.on('GET', '/api/v1/field/me', (options) {
      final auth = options.headers['Authorization'] as String?;
      return auth == 'Bearer $freshToken'
          ? (200, {'ok': true})
          : (401, {'detail': 'stale'});
    });

    final client = _client(adapter, store);
    final response = await client.dio.get('/api/v1/field/me');
    expect(response.statusCode, 200);
  });

  test(
    'concurrent refreshes share one in-flight request and all get the new token',
    () async {
      final expiringSoon = fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(seconds: 30)),
      );
      await store.save(
        accessToken: expiringSoon,
        refreshToken: 'refresh-old',
        loginMode: LoginMode.staff,
      );

      var refreshCalls = 0;
      adapter.on('POST', '/api/v1/auth/refresh', (_) {
        refreshCalls++;
        return (
          200,
          {'access_token': freshToken, 'refresh_token': 'refresh-new'},
        );
      });

      final client = _client(adapter, store);
      final results = await Future.wait([
        client.ensureFreshToken(),
        client.ensureFreshToken(),
        client.ensureFreshToken(),
      ]);

      expect(refreshCalls, 1); // one shared in-flight refresh, not three
      expect(results, everyElement(freshToken));
      expect(await store.refreshToken, 'refresh-new');
    },
  );

  test('refresh failure signals session expiry', () async {
    final expiringSoon = fakeJwt(
      expiry: DateTime.now().toUtc().add(const Duration(seconds: 10)),
    );
    await store.save(
      accessToken: expiringSoon,
      refreshToken: 'refresh-dead',
      loginMode: LoginMode.staff,
    );
    adapter.on(
      'POST',
      '/api/v1/auth/refresh',
      (_) => (401, {'detail': 'Account disabled'}),
    );

    var expired = false;
    final client = _client(adapter, store, onExpired: () => expired = true);
    await client.ensureFreshToken();
    expect(expired, isTrue);
  });

  test('transient refresh failure preserves the session for retry', () async {
    final expiringSoon = fakeJwt(
      expiry: DateTime.now().toUtc().add(const Duration(seconds: 10)),
    );
    await store.save(
      accessToken: expiringSoon,
      refreshToken: 'refresh-live',
      loginMode: LoginMode.staff,
    );
    adapter.on(
      'POST',
      '/api/v1/auth/refresh',
      (_) => (503, {'detail': 'Temporarily unavailable'}),
    );

    var expired = false;
    final client = _client(adapter, store, onExpired: () => expired = true);
    expect(await client.ensureFreshToken(), isNull);

    expect(expired, isFalse);
    expect(await store.refreshToken, 'refresh-live');
    expect(await store.accessToken, expiringSoon);
  });

  test('config gate blocks below min version and merges flags', () async {
    adapter.on(
      'GET',
      '/api/v1/field/config',
      (_) => (
        200,
        {
          'min_app_version': '2.0.0',
          'latest_app_version': '2.1.0',
          'feature_flags': {'vendor_module': true, 'location_sharing': false},
        },
      ),
    );

    final config = await repo.fetchConfig();
    expect(config.upgradeRequired, isTrue);
    expect(config.featureFlags['vendor_module'], isTrue);
  });

  test('config gate allows current version', () async {
    adapter.on(
      'GET',
      '/api/v1/field/config',
      (_) => (
        200,
        {
          'min_app_version': '1.0.0',
          'latest_app_version': '1.0.0',
          'feature_flags': {},
        },
      ),
    );
    final config = await repo.fetchConfig();
    expect(config.upgradeRequired, isFalse);
  });

  test('compareSemver ordering', () {
    expect(compareSemver('1.0.0', '1.0.0'), 0);
    expect(compareSemver('1.0.0', '1.0.1'), lessThan(0));
    expect(compareSemver('1.2.0', '1.10.0'), lessThan(0));
    expect(compareSemver('2.0.0', '1.9.9'), greaterThan(0));
  });
}

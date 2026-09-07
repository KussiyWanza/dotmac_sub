import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:flutter_test/flutter_test.dart';

DioException _error(Object? data, {int statusCode = 409}) {
  final request = RequestOptions(path: '/me/quote-request');
  return DioException(
    requestOptions: request,
    response: Response<Object?>(
      requestOptions: request,
      statusCode: statusCode,
      data: data,
    ),
    type: DioExceptionType.badResponse,
  );
}

void main() {
  test('parses the shared top-level domain error envelope', () {
    final error = ApiException.fromDio(
      _error({
        'code': 'sales.portal_quote.retired',
        'message': 'Online quoting is unavailable.',
        'details': {'reason': 'retired'},
        'request_id': 'req-123',
      }),
    );

    expect(error.message, 'Online quoting is unavailable.');
    expect(error.code, 'sales.portal_quote.retired');
    expect(error.requestId, 'req-123');
    expect(error.statusCode, 409);
  });

  test('continues to parse the legacy detail envelope', () {
    final error = ApiException.fromDio(
      _error({
        'detail': {
          'code': 'PASSWORD_RESET_REQUIRED',
          'message': 'Reset your password.',
        },
      }, statusCode: 428),
    );

    expect(error.message, 'Reset your password.');
    expect(error.isPasswordResetRequired, isTrue);
  });
}

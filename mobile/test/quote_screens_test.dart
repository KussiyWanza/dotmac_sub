import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/features/service/quote_request_screen.dart';
import 'package:dotmac_portal/src/features/service/quotes_screen.dart';
import 'package:dotmac_portal/src/models/quote.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:dotmac_portal/src/repositories/location_repository.dart';
import 'package:dotmac_portal/src/repositories/quotes_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:latlong2/latlong.dart';

QuotesPage _page({
  required bool actionsAvailable,
  List<Quote> quotes = const [],
}) =>
    QuotesPage(
      quotes: quotes,
      total: quotes.length,
      open: quotes.length,
      sourceState:
          actionsAvailable ? QuoteSourceState.native : QuoteSourceState.retired,
      actionsAvailable: actionsAvailable,
      actionsUnavailableMessage:
          actionsAvailable ? null : 'Please contact support to continue.',
    );

Quote _payableQuote() => Quote(
      id: 'quote-1',
      status: 'draft',
      currency: 'NGN',
      total: '50000.00',
      depositAmount: '25000.00',
      depositPaid: false,
      estimateProvisional: false,
      feasibility: QuoteFeasibility(coverage: 'covered', feasible: true),
    );

class _CapturingQuotesRepository extends QuotesRepository {
  _CapturingQuotesRepository() : super(Dio());

  double? latitude;
  double? longitude;
  String? address;
  String? note;

  @override
  Future<Quote> requestQuote({
    required double latitude,
    required double longitude,
    String? address,
    String? region,
    String? note,
  }) async {
    this.latitude = latitude;
    this.longitude = longitude;
    this.address = address;
    this.note = note;
    throw ApiException('Expected test refusal', statusCode: 409);
  }
}

class _LocationRepository extends LocationRepository {
  _LocationRepository() : super(Dio());

  @override
  Future<String?> reverseGeocode(double latitude, double longitude) async {
    return 'Reverse-geocoded address';
  }
}

Widget _app({
  required Widget child,
  required QuotesPage page,
  QuotesRepository? quotesRepository,
}) {
  return ProviderScope(
    overrides: [
      quotesProvider.overrideWith((_) async => page),
      if (quotesRepository != null)
        quotesRepositoryProvider.overrideWithValue(quotesRepository),
      locationRepositoryProvider.overrideWithValue(_LocationRepository()),
    ],
    child: MaterialApp(home: child),
  );
}

void main() {
  testWidgets('quote list hides the request action when its owner disables it',
      (tester) async {
    await tester.pumpWidget(
      _app(
        child: const QuotesScreen(),
        page: _page(actionsAvailable: false, quotes: [_payableQuote()]),
      ),
    );
    await tester.pump();

    expect(find.text('Please contact support to continue.'), findsOneWidget);
    expect(find.text('Request installation'), findsNothing);
    expect(find.textContaining('Pay deposit'), findsNothing);
  });

  testWidgets('direct request route also fails closed when quoting is disabled',
      (tester) async {
    await tester.pumpWidget(
      _app(
        child: const QuoteRequestScreen(),
        page: _page(actionsAvailable: false),
      ),
    );
    await tester.pump();

    expect(find.text('Please contact support to continue.'), findsOneWidget);
    expect(find.byKey(const ValueKey('get-estimate')), findsNothing);
  });

  testWidgets('manual address and notes submit with a required blue pin',
      (tester) async {
    final repository = _CapturingQuotesRepository();
    await tester.pumpWidget(
      _app(
        child: const QuoteRequestScreen(),
        page: _page(actionsAvailable: true),
        quotesRepository: repository,
      ),
    );
    await tester.pump();

    final map = tester.widget<FlutterMap>(find.byType(FlutterMap));
    map.options.onTap!(
      const TapPosition(Offset.zero, Offset.zero),
      const LatLng(9.0564, 7.4986),
    );
    await tester.pump();

    final pin = tester.widget<Icon>(
      find.byKey(const ValueKey('installation-map-pin')),
    );
    final pinContext = tester.element(
      find.byKey(const ValueKey('installation-map-pin')),
    );
    expect(pin.color, Theme.of(pinContext).colorScheme.primary);

    await tester.enterText(
      find.byKey(const ValueKey('installation-address')),
      '12 Mississippi Street, Maitama',
    );
    await tester.enterText(
      find.byKey(const ValueKey('installation-notes')),
      'Blue gate, second floor',
    );
    await tester.tap(find.byKey(const ValueKey('get-estimate')));
    await tester.pump();

    expect(repository.latitude, 9.0564);
    expect(repository.longitude, 7.4986);
    expect(repository.address, '12 Mississippi Street, Maitama');
    expect(repository.note, 'Blue gate, second floor');

    // Let the reverse-geocode debounce finish so no timer outlives the test.
    await tester.pump(const Duration(milliseconds: 500));
  });
}

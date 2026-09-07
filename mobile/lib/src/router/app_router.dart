import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../core/observability.dart';
import '../features/auth/forgot_password_screen.dart';
import '../features/billing/topup_screen.dart';
import '../features/auth/lock_screen.dart';
import '../features/auth/login_screen.dart';
import '../features/auth/mfa_screen.dart';
import '../features/auth/profile_screen.dart';
import '../features/auth/reset_password_screen.dart';
import '../features/auth/sessions_screen.dart';
import '../features/profile/contacts_screen.dart';
import '../features/profile/installation_tracker_screen.dart';
import '../features/profile/refer_and_earn_screen.dart';
import '../features/profile/service_location_screen.dart';
import '../features/profile/technician_track_screen.dart';
import '../features/profile/work_orders_screen.dart';
import '../features/billing/activity_detail_screen.dart';
import '../features/billing/invoice_detail_screen.dart';
import '../features/billing/invoices_screen.dart';
import '../features/billing/payment_detail_screen.dart';
import '../features/billing/transfer_proofs_screen.dart';
import '../features/billing/payment_methods_screen.dart';
import '../features/billing/payment_webview_screen.dart';
import '../features/home/dashboard_screen.dart';
import '../features/home/home_shell.dart';
import '../features/home/notifications_screen.dart';
import '../features/home/splash_screen.dart';
import '../features/reseller/reseller_account_screen.dart';
import '../features/reseller/reseller_accounts_screen.dart';
import '../features/reseller/reseller_billing_screen.dart';
import '../features/reseller/reseller_crm_screen.dart';
import '../features/reseller/reseller_fiber_map_screen.dart';
import '../features/reseller/reseller_home_screen.dart';
import '../features/reseller/reseller_payment_methods_screen.dart';
import '../features/reseller/reseller_profile_screen.dart';
import '../features/reseller/reseller_revenue_screen.dart';
import '../features/reseller/reseller_service_requests_screen.dart';
import '../features/service/add_ons_screen.dart';
import '../features/service/change_plan_screen.dart';
import '../features/service/connection_status_screen.dart';
import '../features/service/data_bundle_screen.dart';
import '../features/service/quote_request_screen.dart';
import '../features/service/quotes_screen.dart';
import '../features/service/service_detail_screen.dart';
import '../features/service/service_route.dart';
import '../features/settings/settings_screen.dart';
import '../features/support/chat_screen.dart';
import '../features/support/create_ticket_screen.dart';
import '../features/support/ticket_detail_screen.dart';
import '../features/support/tickets_screen.dart';
import '../features/service/service_tab_screen.dart';
import '../models/subscription.dart';
import '../providers/auth_controller.dart';
import '../providers/impersonation.dart';

/// Bridges a Riverpod provider to a [Listenable] so GoRouter re-runs its
/// redirect whenever auth state changes.
class _AuthRefresh extends ChangeNotifier {
  _AuthRefresh(Ref ref) {
    ref.listen(authControllerProvider, (_, __) => notifyListeners());
    // Re-run the redirect when impersonation starts/stops so a reseller is
    // returned to their portal the moment "view as customer" ends.
    ref.listen(impersonationProvider, (_, __) => notifyListeners());
  }
}

final routerProvider = Provider<GoRouter>((ref) {
  final refresh = _AuthRefresh(ref);

  return GoRouter(
    navigatorKey: rootNavigatorKey,
    initialLocation: '/splash',
    refreshListenable: refresh,
    observers: [_BreadcrumbNavObserver()],
    redirect: (context, state) {
      final auth = ref.read(authControllerProvider);
      final loc = state.matchedLocation;

      // While bootstrap is resolving, hold on the splash so no authenticated
      // data screen mounts (and fires API calls) before we know the auth state.
      if (!auth.isKnown) return loc == '/splash' ? null : '/splash';

      const publicPaths = {
        '/login',
        '/mfa',
        '/forgot-password',
        '/reset-password',
      };

      if (!auth.isAuthenticated) {
        return publicPaths.contains(loc) ? null : '/login';
      }
      // Authenticated but held behind the biometric lock: stay on /lock,
      // remembering where the lock interrupted so unlock can return there.
      if (auth.locked) {
        if (loc == '/lock') return null;
        ref.read(authControllerProvider.notifier).stashLockReturnLocation(loc);
        return '/lock';
      }

      final isReseller = auth.me?.isReseller ?? false;
      final home = isReseller ? '/reseller' : '/dashboard';
      // Unlocked: go back to wherever the lock interrupted (the redirect
      // re-runs on that location, so the portal checks below still apply),
      // falling back to the portal home when there is nothing stashed.
      if (loc == '/lock') {
        return ref
                .read(authControllerProvider.notifier)
                .takeLockReturnLocation() ??
            home;
      }
      // Authenticated and unlocked: leave the splash/login behind.
      if (loc == '/splash' || loc == '/login') return home;
      // Keep each principal in its own portal even when /auth/me resolves
      // *after* the first route decision. On relaunch the controller shows a
      // cached/empty profile first, so a reseller can briefly land on
      // /dashboard before the real profile loads; once it does, send them to
      // /reseller. Skip while a reseller is impersonating ("view as customer"),
      // which intentionally lives in the customer shell.
      final onReseller = loc.startsWith('/reseller');
      final impersonating = ref.read(impersonationProvider) != null;
      if (isReseller && !onReseller && !impersonating) return '/reseller';
      if (!isReseller && onReseller) return '/dashboard';
      return null;
    },
    routes: [
      GoRoute(path: '/splash', builder: (_, __) => const SplashScreen()),
      GoRoute(path: '/login', builder: (_, __) => const LoginScreen()),
      GoRoute(path: '/lock', builder: (_, __) => const LockScreen()),
      GoRoute(
        path: '/forgot-password',
        builder: (_, __) => const ForgotPasswordScreen(),
      ),
      GoRoute(
        path: '/reset-password',
        builder: (_, state) =>
            ResetPasswordScreen(token: state.uri.queryParameters['token']),
      ),
      GoRoute(
        path: '/mfa',
        builder: (_, state) => MfaScreen(mfaToken: state.extra as String),
      ),
      // Modal money tasks — full-screen on the root navigator (above the
      // bottom-nav shell), entered with context.push so back returns to the
      // originating screen. Everything else (drill-downs) stays inside the
      // shell and keeps the bottom bar.
      GoRoute(
        path: '/topup',
        // `extra == true` (passed by "Add card") pre-enables the Paystack
        // "Save this card" toggle so a top-up doubles as saving a card.
        builder: (_, state) =>
            TopUpScreen(saveCardInitial: state.extra == true),
      ),
      // Connection troubleshooter (outage classifier P4) — reached from the
      // slim Home banner and the Service tab; a drill-in that keeps the shell.
      GoRoute(
        path: '/connection',
        builder: (_, __) => const ConnectionStatusScreen(),
      ),
      // Live technician tracking for an in-progress visit — a full-screen map
      // on its own, reached from the Home visit banner or Help → Visits (kept
      // off the dashboard so Home stays calm).
      GoRoute(
        path: '/track/:id',
        builder: (_, state) =>
            TechnicianTrackScreen(workOrderId: state.pathParameters['id']!),
      ),
      // Live chat now lives INSIDE the Support tab (so the bottom bar stays and
      // it's not a detached full-screen page). Keep /chat as a redirect so push
      // notifications and old deep links land on the nested screen.
      GoRoute(path: '/chat', redirect: (_, __) => '/support/chat'),
      GoRoute(
        path: '/pay',
        builder: (_, state) =>
            PaymentWebViewScreen(args: state.extra as CheckoutArgs),
      ),
      // Self-serve installation quotes (map-pin → estimate → pay deposit).
      GoRoute(path: '/quotes', builder: (_, __) => const QuotesScreen()),
      GoRoute(
        path: '/quotes/request',
        builder: (_, __) => const QuoteRequestScreen(),
      ),
      // Account — identity & settings, reached from the header avatar
      // (AccountAvatarButton) instead of a bottom-nav tab. A top-level route
      // above the shell, so it opens full-screen with a back button. Every
      // /profile/* sub-route is unchanged, so existing pushes, notification
      // deep links, and `context.push('/profile/...')` all keep working.
      GoRoute(
        path: '/profile',
        builder: (_, __) => const ProfileScreen(),
        routes: [
          GoRoute(
            path: 'sessions',
            builder: (_, __) => const SessionsScreen(),
          ),
          GoRoute(
            path: 'payment-methods',
            builder: (_, __) => const PaymentMethodsScreen(),
          ),
          GoRoute(
            path: 'settings',
            builder: (_, __) => const SettingsScreen(),
          ),
          GoRoute(
            path: 'service-location',
            builder: (_, __) => const ServiceLocationScreen(),
          ),
          GoRoute(
            path: 'contacts',
            builder: (_, __) => const ContactsScreen(),
          ),
          GoRoute(
            path: 'refer-and-earn',
            builder: (_, __) => const ReferAndEarnScreen(),
          ),
          GoRoute(
            path: 'installation-progress',
            builder: (_, __) => const InstallationTrackerScreen(),
          ),
          GoRoute(
            path: 'technician-visits',
            builder: (_, __) => const WorkOrdersScreen(),
          ),
        ],
      ),
      // Reseller portal — a standalone landing (resellers manage many customer
      // accounts), outside the customer bottom-nav shell.
      GoRoute(
        path: '/reseller',
        builder: (_, __) => const ResellerHomeScreen(),
        routes: [
          GoRoute(
            path: 'accounts',
            builder: (_, __) => const ResellerAccountsScreen(),
          ),
          GoRoute(
            path: 'billing',
            builder: (_, __) => const ResellerBillingScreen(),
          ),
          GoRoute(
            path: 'chat',
            builder: (_, __) => const ChatScreen(
              sessionEndpoint: '/reseller/chat/session',
              fallbackRoute: '/reseller',
            ),
          ),
          GoRoute(
            path: 'fiber-map',
            builder: (_, __) => const ResellerFiberMapScreen(),
          ),
          GoRoute(
            path: 'service-requests',
            builder: (_, __) => const ResellerServiceRequestsScreen(),
          ),
          GoRoute(
            path: 'quotes',
            builder: (_, __) => const ResellerCrmScreen(),
          ),
          GoRoute(
            path: 'profile',
            builder: (_, __) => const ResellerProfileScreen(),
          ),
          GoRoute(
            path: 'payment-methods',
            builder: (_, __) => const ResellerPaymentMethodsScreen(),
          ),
          // Reuses the customer Contacts screen: /me/contacts is self-scoped
          // and works for reseller users (they're Subscribers too).
          GoRoute(path: 'contacts', builder: (_, __) => const ContactsScreen()),
          GoRoute(
            path: 'revenue',
            builder: (_, __) => const ResellerRevenueScreen(),
          ),
          GoRoute(
            path: 'accounts/:id',
            builder: (_, state) => ResellerAccountScreen(
              accountId: state.pathParameters['id']!,
              title: state.extra as String?,
            ),
          ),
        ],
      ),
      // Authenticated shell with bottom navigation. An indexed-stack shell
      // keeps every tab's navigation stack and widget state alive across tab
      // switches (scroll positions, the Billing sub-tabs, filters).
      StatefulShellRoute.indexedStack(
        builder: (_, __, navigationShell) =>
            HomeShell(navigationShell: navigationShell),
        branches: [
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/dashboard',
                builder: (_, __) => const DashboardScreen(),
                routes: [
                  GoRoute(
                    path: 'notifications',
                    builder: (_, __) => const NotificationsScreen(),
                  ),
                ],
              ),
              // Service drill-down + its sub-screens, in the Home branch (it is
              // entered from the dashboard). The originating screen passes the
              // Subscription via `extra`; deep links resolve the id from the
              // subscriptions cache (see ServiceRoute).
              GoRoute(
                path: '/service/:id',
                builder: (_, state) => ServiceRoute(
                  id: state.pathParameters['id']!,
                  initial: state.extra as Subscription?,
                  builder: (s) => ServiceDetailScreen(service: s),
                ),
                routes: [
                  GoRoute(
                    path: 'change-plan',
                    builder: (_, state) => ServiceRoute(
                      id: state.pathParameters['id']!,
                      initial: state.extra as Subscription?,
                      builder: (s) => ChangePlanScreen(service: s),
                    ),
                  ),
                  GoRoute(
                    path: 'addons',
                    builder: (_, state) => ServiceRoute(
                      id: state.pathParameters['id']!,
                      initial: state.extra as Subscription?,
                      builder: (s) => AddOnsScreen(service: s),
                    ),
                  ),
                  GoRoute(
                    path: 'buy-data',
                    builder: (_, state) => ServiceRoute(
                      id: state.pathParameters['id']!,
                      initial: state.extra as Subscription?,
                      builder: (s) => DataBundleScreen(service: s),
                    ),
                  ),
                ],
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              // Path kept as /usage so old deep links and notifications keep working;
              // the tab itself is now the Service tab (plan + data + add-ons + usage).
              GoRoute(
                path: '/usage',
                builder: (_, __) => const ServiceTabScreen(),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/billing',
                builder: (_, __) => const InvoicesScreen(),
                routes: [
                  GoRoute(
                    path: 'transfer-proofs',
                    builder: (_, __) => const TransferProofsScreen(),
                  ),
                  GoRoute(
                    path: 'invoices/:id',
                    builder: (_, state) => InvoiceDetailScreen(
                      invoiceId: state.pathParameters['id']!,
                    ),
                  ),
                  GoRoute(
                    path: 'payments/:id',
                    builder: (_, state) => PaymentDetailScreen(
                      paymentId: state.pathParameters['id']!,
                    ),
                  ),
                  GoRoute(
                    path: 'activity/:id',
                    builder: (_, state) => ActivityDetailScreen(
                      entryId: state.pathParameters['id']!,
                    ),
                  ),
                ],
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/support',
                builder: (_, __) => const TicketsScreen(),
                routes: [
                  // Live chat, nested so the bottom nav stays and back returns to
                  // the ticket list.
                  GoRoute(path: 'chat', builder: (_, __) => const ChatScreen()),
                  GoRoute(
                    path: 'new',
                    builder: (_, __) => const CreateTicketScreen(),
                  ),
                  GoRoute(
                    path: ':id',
                    builder: (_, state) => TicketDetailScreen(
                      ticketId: state.pathParameters['id']!,
                    ),
                  ),
                ],
              ),
            ],
          ),
        ],
      ),
    ],
  );
});

/// Root navigator key. Drill-down routes live inside the shell (bottom bar
/// stays); only modal money tasks (/topup, /pay) sit above it as top-level
/// routes.
final rootNavigatorKey = GlobalKey<NavigatorState>();

/// Leaves a breadcrumb on each navigation so crash reports show the screen
/// trail that led to the error.
class _BreadcrumbNavObserver extends NavigatorObserver {
  void _log(String action, Route<dynamic>? route) {
    final name = route?.settings.name;
    if (name != null) Log.breadcrumb('$action $name', category: 'navigation');
  }

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) =>
      _log('push', route);

  @override
  void didPop(Route<dynamic> route, Route<dynamic>? previousRoute) =>
      _log('pop', route);

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) =>
      _log('replace', newRoute);
}

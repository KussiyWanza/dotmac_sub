import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../core/api/token_store.dart' show LoginMode;
import '../features/auth/auth_state.dart';
import '../features/auth/login_screen.dart';
import '../features/auth/mfa_screen.dart';
import '../features/expenses/expenses_screen.dart';
import '../features/execution/completion_wizard.dart';
import '../features/jobs/job_chat_screen.dart';
import '../features/jobs/job_detail_screen.dart';
import '../features/jobs/jobs_providers.dart';
import '../features/jobs/work_order_evidence_map_screen.dart';
import '../features/location/location_tracking_controller.dart';
import '../features/manager/manager_providers.dart';
import '../features/manager/manager_screen.dart';
import '../features/materials/materials_screen.dart';
import '../features/profile/profile_screen.dart';
import '../features/schedule/schedule_screen.dart';
import '../features/today/map_screen.dart';
import '../features/today/today_screen.dart';
import '../features/vendor/vendor_map_screen.dart';

/// App shell: login gate + 4-tab bottom navigation per the visual plan.
GoRouter buildRouter(Ref ref) {
  final listenable = ValueNotifier(0);
  ref.listen(authControllerProvider, (_, _) => listenable.value++);

  return GoRouter(
    restorationScopeId: 'dotmac-field-router',
    initialLocation: '/today',
    refreshListenable: listenable,
    redirect: (context, state) {
      final auth = ref.read(authControllerProvider);
      final atRestore = state.matchedLocation == '/restore';
      final atLogin = state.matchedLocation == '/login';
      final atMfa = state.matchedLocation == '/mfa';
      final atUpgrade = state.matchedLocation == '/upgrade';
      return switch (auth) {
        RestoringSession() => atRestore ? null : '/restore',
        Unauthenticated() => atLogin ? null : '/login',
        AwaitingMfa() => atMfa ? null : '/mfa',
        UpgradeRequired() => atUpgrade ? null : '/upgrade',
        Authenticated() =>
          (atRestore || atLogin || atMfa || atUpgrade) ? '/today' : null,
      };
    },
    routes: [
      GoRoute(path: '/restore', builder: (_, _) => const _RestoreScreen()),
      GoRoute(path: '/login', builder: (_, _) => const LoginScreen()),
      GoRoute(path: '/mfa', builder: (_, _) => const MfaScreen()),
      GoRoute(
        path: '/upgrade',
        builder: (_, _) => const UpgradeRequiredScreen(),
      ),
      GoRoute(
        path: '/jobs/:id',
        builder: (_, state) =>
            JobDetailScreen(jobId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/jobs/:id/complete',
        builder: (_, state) =>
            _CompletionRoute(jobId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/jobs/:id/chat',
        builder: (_, state) =>
            JobChatScreen(jobId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/jobs/:id/fiber-evidence',
        builder: (_, state) => WorkOrderEvidenceMapScreen(
          workOrderPublicId: state.pathParameters['id']!,
        ),
      ),
      GoRoute(
        path: '/materials/new',
        builder: (_, state) => NewMaterialRequestScreen(
          initialWorkOrderId: state.uri.queryParameters['workOrderId'],
          initialWorkOrderLabel: state.uri.queryParameters['workOrderLabel'],
          initialProjectId: state.uri.queryParameters['projectId'],
          initialTicketId: state.uri.queryParameters['ticketId'],
        ),
      ),
      GoRoute(
        path: '/materials/:id',
        builder: (_, state) =>
            MaterialRequestDetailScreen(id: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/expenses/new',
        builder: (_, state) => NewExpenseRequestScreen(
          initialWorkOrderId: state.uri.queryParameters['workOrderId'],
          initialWorkOrderLabel: state.uri.queryParameters['workOrderLabel'],
          initialProjectId: state.uri.queryParameters['projectId'],
          initialTicketId: state.uri.queryParameters['ticketId'],
        ),
      ),
      GoRoute(
        path: '/expenses/:id',
        builder: (_, state) =>
            ExpenseRequestDetailScreen(id: state.pathParameters['id']!),
      ),
      StatefulShellRoute.indexedStack(
        builder: (context, state, shell) => _AppShell(shell: shell),
        branches: [
          StatefulShellBranch(
            routes: [
              GoRoute(path: '/today', builder: (_, _) => const _HomeSwitch()),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/map',
                builder: (_, state) =>
                    _MapSwitch(focusJobId: state.uri.queryParameters['jobId']),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/schedule',
                builder: (_, _) => const _ScheduleSwitch(),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/materials',
                builder: (_, _) => const MaterialsScreen(),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/expenses',
                builder: (_, _) => const _ExpensesSwitch(),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: '/profile',
                builder: (_, _) => const ProfileScreen(),
              ),
            ],
          ),
        ],
      ),
    ],
  );
}

final routerProvider = Provider<GoRouter>(buildRouter);

class _CompletionRoute extends ConsumerWidget {
  const _CompletionRoute({required this.jobId});

  final String jobId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return ref
        .watch(jobDetailProvider(jobId))
        .when(
          data: (detail) => CompletionWizard(
            jobId: jobId,
            requirements: detail.completionRequirements,
            existingPhotoCount: detail.completionPhotoCount,
            hasExistingSignature: detail.hasCompletionSignature,
          ),
          loading: () =>
              const Scaffold(body: Center(child: CircularProgressIndicator())),
          error: (_, _) => Scaffold(
            appBar: AppBar(),
            body: const Center(child: Text('Could not restore this job')),
          ),
        );
  }
}

class _RestoreScreen extends StatelessWidget {
  const _RestoreScreen();

  @override
  Widget build(BuildContext context) {
    return const Scaffold(body: Center(child: CircularProgressIndicator()));
  }
}

/// Work-order processing is shared for staff and vendor crews; the backend
/// scopes each mode to its own assigned jobs. Managers still get the dashboard.
class _HomeSwitch extends ConsumerWidget {
  const _HomeSwitch();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (isManagerProfile(ref.watch(managerProfileProvider))) {
      return const ManagerDashboardScreen();
    }
    return const TodayScreen();
  }
}

/// The Map tab shows vendors their vendor-scoped nearby plant; techs get the
/// full technician map (job pins + editable assets).
class _MapSwitch extends ConsumerWidget {
  const _MapSwitch({this.focusJobId});

  final String? focusJobId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authControllerProvider);
    if (auth is Authenticated && auth.mode == LoginMode.vendor) {
      return const VendorMapScreen();
    }
    if (isManagerProfile(ref.watch(managerProfileProvider))) {
      return const ManagerTeamMapScreen();
    }
    return MapScreen(key: ValueKey(focusJobId), focusJobId: focusJobId);
  }
}

class _ScheduleSwitch extends ConsumerWidget {
  const _ScheduleSwitch();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (isManagerProfile(ref.watch(managerProfileProvider))) {
      return const ManagerDispatchScreen();
    }
    return const ScheduleScreen();
  }
}

class _ExpensesSwitch extends ConsumerWidget {
  const _ExpensesSwitch();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (isManagerProfile(ref.watch(managerProfileProvider))) {
      return const ManagerExpenseReviewScreen();
    }
    return const ExpensesScreen();
  }
}

/// A bottom-nav destination bound to a shell branch index. The visible set
/// differs by login mode, but every entry maps to the same fixed branch so
/// `shell.goBranch` stays correct regardless of what's shown.
class _NavItem {
  const _NavItem(this.branchIndex, this.icon, this.label);
  final int branchIndex;
  final IconData icon;
  final String label;
}

// Branch order (see StatefulShellRoute above):
// 0 Today/Vendor map · 1 Map · 2 Schedule · 3 Materials · 4 Expenses ·
// 5 Profile
const _staffNav = [
  _NavItem(0, Icons.assignment_outlined, 'Today'),
  _NavItem(1, Icons.map_outlined, 'Map'),
  _NavItem(2, Icons.calendar_today_outlined, 'Schedule'),
  _NavItem(3, Icons.inventory_2_outlined, 'Materials'),
  _NavItem(4, Icons.receipt_long_outlined, 'Expenses'),
  _NavItem(5, Icons.person_outline, 'Profile'),
];

// Vendors process sub-native work orders through the same execution tabs. The
// Map branch remains vendor-scoped via _MapSwitch.
const _vendorNav = [
  _NavItem(0, Icons.assignment_outlined, 'Today'),
  _NavItem(1, Icons.map_outlined, 'Map'),
  _NavItem(2, Icons.calendar_today_outlined, 'Schedule'),
  _NavItem(3, Icons.inventory_2_outlined, 'Materials'),
  _NavItem(4, Icons.receipt_long_outlined, 'Expenses'),
  _NavItem(5, Icons.person_outline, 'Profile'),
];

// Managers keep the same branch set but re-skinned: the Today branch hosts
// the dashboard, Map becomes the team map, Schedule becomes dispatch, and
// Expenses becomes the approvals queue.
const _managerNav = [
  _NavItem(0, Icons.dashboard_outlined, 'Dashboard'),
  _NavItem(1, Icons.map_outlined, 'Team'),
  _NavItem(2, Icons.assignment_ind_outlined, 'Dispatch'),
  _NavItem(4, Icons.fact_check_outlined, 'Approvals'),
  _NavItem(5, Icons.person_outline, 'Profile'),
];

class _AppShell extends ConsumerWidget {
  const _AppShell({required this.shell});

  final StatefulNavigationShell shell;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authControllerProvider);
    final isVendor = auth is Authenticated && auth.mode == LoginMode.vendor;
    final isManager =
        !isVendor && isManagerProfile(ref.watch(managerProfileProvider));
    final items = isVendor
        ? _vendorNav
        : isManager
        ? _managerNav
        : _staffNav;
    // Map the active branch to its position in the visible set (0 if the
    // current branch is hidden for this mode).
    final selected = items.indexWhere(
      (i) => i.branchIndex == shell.currentIndex,
    );
    return Scaffold(
      body: LocationTrackingHost(child: shell),
      bottomNavigationBar: NavigationBar(
        selectedIndex: selected < 0 ? 0 : selected,
        onDestinationSelected: (pos) => shell.goBranch(items[pos].branchIndex),
        destinations: [
          for (final item in items)
            NavigationDestination(icon: Icon(item.icon), label: item.label),
        ],
      ),
    );
  }
}

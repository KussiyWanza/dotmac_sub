import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';
import 'package:latlong2/latlong.dart';

import '../../app/theme.dart';
import '../../app/widgets/status_pill.dart';
import '../../core/location/map_coordinates.dart';
import '../expenses/expense_models.dart';
import 'manager_providers.dart';

class ManagerDashboardScreen extends ConsumerWidget {
  const ManagerDashboardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final profile = ref.watch(managerProfileProvider);
    final summary = ref.watch(managerSummaryProvider);
    return Scaffold(
      appBar: AppBar(
        title: Text(
          profile.valueOrNull == null
              ? 'Field manager'
              : 'Hi, ${_firstName(profile.valueOrNull!.name)}',
        ),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            onPressed: () {
              ref
                ..invalidate(managerProfileProvider)
                ..invalidate(managerSummaryProvider)
                ..invalidate(managerTechniciansProvider)
                ..invalidate(managerJobsProvider)
                ..invalidate(managerExpensesProvider);
            },
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref
            ..invalidate(managerSummaryProvider)
            ..invalidate(managerTechniciansProvider)
            ..invalidate(managerJobsProvider)
            ..invalidate(managerExpensesProvider);
        },
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: [
            Text(
              'Operations dashboard',
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w800),
            ),
            const SizedBox(height: 6),
            Text(
              'Dispatch load, active technicians, and approvals.',
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: AppColors.subdued(context),
              ),
            ),
            const SizedBox(height: 18),
            summary.when(
              data: (data) => Column(
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: _MetricCard(
                          icon: Icons.engineering_outlined,
                          label: 'Live techs',
                          value: '${data.techniciansLive}',
                          detail: '${data.techniciansSharing} sharing',
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: _MetricCard(
                          icon: Icons.assignment_outlined,
                          label: 'Open jobs',
                          value: '${data.openJobs}',
                          detail: '${data.unassignedJobs} unassigned',
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      Expanded(
                        child: _MetricCard(
                          icon: Icons.receipt_long_outlined,
                          label: 'Approvals',
                          value: '${data.pendingExpenses}',
                          detail: 'pending expenses',
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: _MetricCard(
                          icon: Icons.groups_outlined,
                          label: 'Team',
                          value: '${data.techniciansTotal}',
                          detail: 'active profiles',
                        ),
                      ),
                    ],
                  ),
                ],
              ),
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (_, _) =>
                  const _InlineError(message: 'Could not load manager summary'),
            ),
            const SizedBox(height: 18),
            _QuickActions(
              canViewTeamMap: profile.valueOrNull?.canViewTeamMap == true,
            ),
          ],
        ),
      ),
    );
  }
}

class ManagerTeamMapScreen extends ConsumerStatefulWidget {
  const ManagerTeamMapScreen({super.key, this.showTiles = true});

  /// Disabled in widget tests so no tile HTTP requests are made.
  final bool showTiles;

  @override
  ConsumerState<ManagerTeamMapScreen> createState() =>
      _ManagerTeamMapScreenState();
}

enum _TeamFilter { all, live, stale, notSharing }

class _ManagerTeamMapScreenState extends ConsumerState<ManagerTeamMapScreen>
    with WidgetsBindingObserver {
  static const _refreshInterval = Duration(seconds: 30);
  static const _selectedTechnicianZoom = 17.0;

  final _mapController = MapController();
  final _scrollController = ScrollController();
  Timer? _refreshTimer;
  bool _isForeground = true;
  bool _isVisible = true;
  String _searchQuery = '';
  _TeamFilter _filter = _TeamFilter.all;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _refreshTimer = Timer.periodic(_refreshInterval, (_) {
      if (mounted && _isForeground && _isVisible) {
        ref.invalidate(managerTeamMapProvider);
      }
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _isForeground = state == AppLifecycleState.resumed;
    if (_isForeground && mounted && _isVisible) {
      ref.invalidate(managerTeamMapProvider);
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _refreshTimer?.cancel();
    _mapController.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      await Future.wait([
        ref.refresh(managerTeamMapProvider.future),
        ref.refresh(managerTechniciansProvider.future),
      ]);
    } catch (_) {
      // Each failed provider renders its own retryable inline state.
    }
  }

  @override
  Widget build(BuildContext context) {
    _isVisible = TickerMode.valuesOf(context).enabled;
    final mapFeed = ref.watch(managerTeamMapProvider);
    final technicians = ref.watch(managerTechniciansProvider);
    final positions =
        mapFeed.valueOrNull?.positions
            .where(
              (position) =>
                  isValidMapCoordinate(position.latitude, position.longitude),
            )
            .toList() ??
        const <ManagerTeamMapPosition>[];
    final positionByPersonId = {
      for (final position in positions) position.personId: position,
    };
    final visibleTechnicians =
        [
          ...?technicians.valueOrNull?.where(
            (technician) => _matchesTechnician(
              technician,
              positionByPersonId[technician.personId],
            ),
          ),
        ]..sort(
          (left, right) => _compareTechnicians(left, right, positionByPersonId),
        );

    return Scaffold(
      appBar: AppBar(
        title: const Text('Team location'),
        actions: [
          IconButton(
            tooltip: 'Refresh team locations',
            onPressed: _refresh,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: CustomScrollView(
          controller: _scrollController,
          physics: const AlwaysScrollableScrollPhysics(),
          slivers: [
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
              sliver: SliverToBoxAdapter(
                child: _buildMapSurface(mapFeed, positions),
              ),
            ),
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
              sliver: SliverToBoxAdapter(
                child: _TeamMapControls(
                  filter: _filter,
                  total: technicians.valueOrNull?.length,
                  onQueryChanged: (value) =>
                      setState(() => _searchQuery = value),
                  onFilterChanged: (value) => setState(() => _filter = value),
                ),
              ),
            ),
            ..._technicianSlivers(
              technicians,
              visibleTechnicians,
              positionByPersonId,
            ),
            const SliverToBoxAdapter(child: SizedBox(height: 24)),
          ],
        ),
      ),
    );
  }

  Widget _buildMapSurface(
    AsyncValue<ManagerTeamMapFeed> state,
    List<ManagerTeamMapPosition> positions,
  ) {
    final feed = state.valueOrNull;
    if (feed == null && state.isLoading) {
      return const _TeamMapMessage(child: CircularProgressIndicator());
    }
    if (feed == null) {
      return _TeamMapMessage(
        child: _RetryMessage(
          message: 'Could not load team locations',
          onRetry: () => ref.invalidate(managerTeamMapProvider),
        ),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _TeamMapPanel(
          positions: positions,
          mapController: _mapController,
          showTiles: widget.showTiles,
          onPositionTap: _showPosition,
        ),
        if (state.hasError)
          _InlineRefreshWarning(
            message: 'Location refresh failed. Showing the last update.',
            onRetry: () => ref.invalidate(managerTeamMapProvider),
          ),
        const SizedBox(height: 8),
        Text(
          '${positions.where((item) => item.isLive).length} live'
          ' · ${positions.where((item) => !item.isLive).length} stale'
          ' · updated ${_relativeTime(feed.receivedAt)}'
          ' · live window ${feed.staleAfterSeconds}s',
          style: Theme.of(
            context,
          ).textTheme.bodySmall?.copyWith(color: AppColors.subdued(context)),
        ),
      ],
    );
  }

  List<Widget> _technicianSlivers(
    AsyncValue<List<ManagerTechnician>> state,
    List<ManagerTechnician> visible,
    Map<String, ManagerTeamMapPosition> positionByPersonId,
  ) {
    if (state.valueOrNull == null && state.isLoading) {
      return const [
        SliverToBoxAdapter(
          child: Padding(
            padding: EdgeInsets.all(32),
            child: Center(child: CircularProgressIndicator()),
          ),
        ),
      ];
    }
    if (state.valueOrNull == null) {
      return [
        SliverToBoxAdapter(
          child: _RetryMessage(
            message: 'Could not load the technician roster',
            onRetry: () => ref.invalidate(managerTechniciansProvider),
          ),
        ),
      ];
    }
    if (visible.isEmpty) {
      return [
        SliverToBoxAdapter(
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 40),
            child: Center(
              child: Text(
                _searchQuery.trim().isEmpty && _filter == _TeamFilter.all
                    ? 'No active technician profiles'
                    : 'No technicians match these filters',
              ),
            ),
          ),
        ),
      ];
    }
    return [
      SliverPadding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        sliver: SliverList.builder(
          itemCount: visible.length,
          itemBuilder: (context, index) {
            final technician = visible[index];
            final position = positionByPersonId[technician.personId];
            return _TechnicianTile(
              technician: technician,
              position: position,
              onTap: () => unawaited(
                _showTechnician(technician, position, bringMapIntoView: true),
              ),
            );
          },
        ),
      ),
    ];
  }

  bool _matchesTechnician(
    ManagerTechnician technician,
    ManagerTeamMapPosition? position,
  ) {
    final matchesFilter = switch (_filter) {
      _TeamFilter.all => true,
      _TeamFilter.live => position?.isLive == true,
      _TeamFilter.stale => position != null && !position.isLive,
      _TeamFilter.notSharing => !technician.locationSharingEnabled,
    };
    if (!matchesFilter) return false;
    final query = _searchQuery.trim().toLowerCase();
    if (query.isEmpty) return true;
    return [
      technician.name,
      technician.title,
      technician.region,
      technician.activeWorkOrderTitle,
    ].whereType<String>().any((value) => value.toLowerCase().contains(query));
  }

  void _showPosition(ManagerTeamMapPosition position) {
    ManagerTechnician? technician;
    for (final item
        in ref.read(managerTechniciansProvider).valueOrNull ??
            const <ManagerTechnician>[]) {
      if (item.personId == position.personId) {
        technician = item;
        break;
      }
    }
    unawaited(_showTechnician(technician, position));
  }

  Future<void> _showTechnician(
    ManagerTechnician? technician,
    ManagerTeamMapPosition? position, {
    bool bringMapIntoView = false,
  }) async {
    if (position != null && bringMapIntoView) {
      await _bringMapIntoView();
    }
    if (!mounted) return;
    if (position != null) _focusPosition(position);
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => _TechnicianLocationSheet(
        technician: technician,
        position: position,
        onOpenDispatch: technician?.activeWorkOrderTitle == null
            ? null
            : () {
                Navigator.of(sheetContext).pop();
                context.go('/schedule');
              },
      ),
    );
  }

  Future<void> _bringMapIntoView() async {
    if (!_scrollController.hasClients) return;
    await _scrollController.animateTo(
      _scrollController.position.minScrollExtent,
      duration: const Duration(milliseconds: 250),
      curve: Curves.easeOutCubic,
    );
    await WidgetsBinding.instance.endOfFrame;
  }

  void _focusPosition(ManagerTeamMapPosition position) {
    _mapController.move(
      LatLng(position.latitude, position.longitude),
      _selectedTechnicianZoom,
    );
  }

  int _compareTechnicians(
    ManagerTechnician left,
    ManagerTechnician right,
    Map<String, ManagerTeamMapPosition> positions,
  ) {
    int rank(ManagerTechnician technician) {
      final position = positions[technician.personId];
      if (position?.isLive == true) return 0;
      if (position != null) return 1;
      if (technician.locationSharingEnabled) return 2;
      return 3;
    }

    final rankComparison = rank(left).compareTo(rank(right));
    return rankComparison != 0
        ? rankComparison
        : left.name.toLowerCase().compareTo(right.name.toLowerCase());
  }
}

class ManagerDispatchScreen extends ConsumerWidget {
  const ManagerDispatchScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final jobs = ref.watch(managerJobsProvider);
    final technicians = ref.watch(managerTechniciansProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Dispatch')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref
            ..invalidate(managerJobsProvider)
            ..invalidate(managerTechniciansProvider);
        },
        child: jobs.when(
          data: (items) => ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(16),
            children: [
              Text(
                'Open work orders',
                style: Theme.of(
                  context,
                ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w800),
              ),
              const SizedBox(height: 8),
              if (items.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 48),
                  child: Center(child: Text('No open jobs')),
                )
              else
                for (final job in items)
                  _DispatchJobCard(
                    job: job,
                    technicians: technicians.valueOrNull ?? const [],
                  ),
            ],
          ),
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (_, _) => const Center(child: Text('Could not load jobs')),
        ),
      ),
    );
  }
}

class ManagerExpenseReviewScreen extends ConsumerStatefulWidget {
  const ManagerExpenseReviewScreen({super.key, this.embedded = false});

  final bool embedded;

  @override
  ConsumerState<ManagerExpenseReviewScreen> createState() =>
      _ManagerExpenseReviewScreenState();
}

class _ManagerExpenseReviewScreenState
    extends ConsumerState<ManagerExpenseReviewScreen> {
  List<ExpenseRequest>? _lastLoadedExpenses;
  final _resolvedExpenseIds = <String>{};
  late final ProviderSubscription<AsyncValue<List<ExpenseRequest>>>
  _expensesSubscription;

  @override
  void initState() {
    super.initState();
    _expensesSubscription = ref.listenManual(managerExpensesProvider, (
      _,
      next,
    ) {
      final items = next.valueOrNull;
      if (items == null || !mounted) return;
      setState(() {
        _lastLoadedExpenses = items;
        _resolvedExpenseIds.removeWhere(
          (id) =>
              items.any((item) => item.id == id && item.status != 'submitted'),
        );
      });
    });
  }

  @override
  void dispose() {
    _expensesSubscription.close();
    super.dispose();
  }

  void _markResolved(String id) {
    if (!mounted) return;
    setState(() => _resolvedExpenseIds.add(id));
  }

  @override
  Widget build(BuildContext context) {
    final expenses = ref.watch(managerExpensesProvider);
    final latestItems = expenses.valueOrNull ?? _lastLoadedExpenses;
    final visibleItems = latestItems
        ?.where((item) => !_resolvedExpenseIds.contains(item.id))
        .toList();
    final canPayExpenses =
        ref.watch(managerProfileProvider).valueOrNull?.canPayExpenses == true;

    final body = RefreshIndicator(
      onRefresh: () async => ref.invalidate(managerExpensesProvider),
      child: switch (visibleItems) {
        final items? => ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: [
            if (expenses.hasError) ...[
              _ApprovalRefreshError(
                onRetry: () => ref.invalidate(managerExpensesProvider),
              ),
              const SizedBox(height: 12),
            ],
            if (items.isEmpty)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 48),
                child: Center(child: Text('No team expenses')),
              )
            else ...[
              _ExpenseSectionTitle(
                title: 'Pending approval',
                count: items.where((item) => item.status == 'submitted').length,
              ),
              for (final request in items.where(
                (item) => item.status == 'submitted',
              ))
                _ExpenseApprovalCard(
                  request: request,
                  canPay: canPayExpenses,
                  onResolved: _markResolved,
                ),
              const SizedBox(height: 12),
              _ExpenseSectionTitle(
                title: 'Approved for payment',
                count: items.where((item) => item.status == 'approved').length,
              ),
              for (final request in items.where(
                (item) => item.status == 'approved',
              ))
                _ExpenseApprovalCard(
                  request: request,
                  canPay: canPayExpenses,
                  onResolved: _markResolved,
                ),
              const SizedBox(height: 12),
              _ExpenseSectionTitle(
                title: 'History',
                count: items
                    .where(
                      (item) =>
                          item.status != 'submitted' &&
                          item.status != 'approved',
                    )
                    .length,
              ),
              for (final request in items.where(
                (item) =>
                    item.status != 'submitted' && item.status != 'approved',
              ))
                _ExpenseApprovalCard(
                  request: request,
                  canPay: canPayExpenses,
                  onResolved: _markResolved,
                ),
            ],
          ],
        ),
        null when expenses.isLoading => const Center(
          child: CircularProgressIndicator(),
        ),
        null => ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: [
            _ApprovalRefreshError(
              initialLoad: true,
              onRetry: () => ref.invalidate(managerExpensesProvider),
            ),
          ],
        ),
      },
    );
    if (widget.embedded) return body;
    return Scaffold(
      appBar: AppBar(title: const Text('Expenses')),
      body: body,
    );
  }
}

class _ApprovalRefreshError extends StatelessWidget {
  const _ApprovalRefreshError({
    required this.onRetry,
    this.initialLoad = false,
  });

  final VoidCallback onRetry;
  final bool initialLoad;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Row(
          children: [
            const Icon(Icons.cloud_off_outlined),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                initialLoad
                    ? 'Could not load expense approvals.'
                    : 'Could not refresh approvals. Showing the last loaded results.',
              ),
            ),
            TextButton(onPressed: onRetry, child: const Text('Retry')),
          ],
        ),
      ),
    );
  }
}

class _QuickActions extends StatelessWidget {
  const _QuickActions({required this.canViewTeamMap});

  final bool canViewTeamMap;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (canViewTeamMap) ...[
          _ActionTile(
            icon: Icons.map_outlined,
            title: 'Team location',
            subtitle: 'Live sharing status and current work context',
            onTap: () => context.go('/map'),
          ),
          const SizedBox(height: 10),
        ],
        _ActionTile(
          icon: Icons.assignment_ind_outlined,
          title: 'Dispatch queue',
          subtitle: 'Assign open jobs to available technicians',
          onTap: () => context.go('/schedule'),
        ),
      ],
    );
  }
}

class _MetricCard extends StatelessWidget {
  const _MetricCard({
    required this.icon,
    required this.label,
    required this.value,
    required this.detail,
  });

  final IconData icon;
  final String label;
  final String value;
  final String detail;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, color: AppColors.primary),
            const SizedBox(height: 14),
            Text(
              value,
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w900),
            ),
            const SizedBox(height: 4),
            Text(label, style: const TextStyle(fontWeight: FontWeight.w700)),
            const SizedBox(height: 2),
            Text(
              detail,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: AppColors.subdued(context),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ActionTile extends StatelessWidget {
  const _ActionTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    this.onTap,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        leading: Icon(icon, color: AppColors.primary),
        title: Text(title),
        subtitle: Text(subtitle),
        trailing: const Icon(Icons.chevron_right),
        onTap: onTap,
      ),
    );
  }
}

class _TeamMapPanel extends StatelessWidget {
  const _TeamMapPanel({
    required this.positions,
    required this.mapController,
    required this.showTiles,
    required this.onPositionTap,
  });

  final List<ManagerTeamMapPosition> positions;
  final MapController mapController;
  final bool showTiles;
  final ValueChanged<ManagerTeamMapPosition> onPositionTap;

  @override
  Widget build(BuildContext context) {
    final points = [
      for (final position in positions)
        LatLng(position.latitude, position.longitude),
    ];
    final initialCenter = points.isEmpty ? defaultMapCenter : points.first;
    return Card(
      clipBehavior: Clip.antiAlias,
      child: SizedBox(
        height: 330,
        child: Stack(
          children: [
            Positioned.fill(
              child: FlutterMap(
                mapController: mapController,
                options: MapOptions(
                  initialCenter: initialCenter,
                  initialZoom: points.length == 1 ? 15 : 11,
                  initialCameraFit: points.length > 1
                      ? CameraFit.coordinates(
                          coordinates: points,
                          padding: const EdgeInsets.all(42),
                          maxZoom: 15,
                        )
                      : null,
                  cameraConstraint: finiteMapCameraConstraint,
                ),
                children: [
                  if (showTiles)
                    TileLayer(
                      urlTemplate:
                          'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                      userAgentPackageName: 'io.dotmac.dotmac_field',
                    ),
                  MarkerLayer(
                    markers: [
                      for (final position in positions)
                        Marker(
                          point: LatLng(position.latitude, position.longitude),
                          width: 48,
                          height: 48,
                          child: _TeamMapMarker(
                            position: position,
                            onTap: () => onPositionTap(position),
                          ),
                        ),
                    ],
                  ),
                  if (showTiles)
                    const Align(
                      alignment: Alignment.bottomLeft,
                      child: Padding(
                        padding: EdgeInsets.all(4),
                        child: Text(
                          '© OpenStreetMap contributors',
                          style: TextStyle(fontSize: 10),
                        ),
                      ),
                    ),
                ],
              ),
            ),
            if (positions.isEmpty)
              ColoredBox(
                color: AppColors.surface(context).withValues(alpha: 0.88),
                child: const Center(
                  child: Padding(
                    padding: EdgeInsets.all(24),
                    child: Text(
                      'No technicians are currently sharing a mapped location',
                      textAlign: TextAlign.center,
                    ),
                  ),
                ),
              ),
            Positioned(
              left: 12,
              top: 12,
              child: DecoratedBox(
                decoration: BoxDecoration(
                  color: AppColors.surface(context).withValues(alpha: 0.92),
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: AppColors.border(context)),
                ),
                child: Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 8,
                  ),
                  child: Text(
                    '${positions.where((item) => item.isLive).length} live'
                    ' · ${positions.where((item) => !item.isLive).length} stale',
                    style: const TextStyle(fontWeight: FontWeight.w800),
                  ),
                ),
              ),
            ),
            if (positions.isNotEmpty)
              Positioned(
                right: 12,
                top: 12,
                child: IconButton.filledTonal(
                  tooltip: 'Fit all technicians',
                  onPressed: () => _fitPositions(mapController, points),
                  icon: const Icon(Icons.center_focus_strong),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _TeamMapMarker extends StatelessWidget {
  const _TeamMapMarker({required this.position, required this.onTap});

  final ManagerTeamMapPosition position;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final color = _positionColor(context, position);
    final status = _positionStatus(position);
    return Semantics(
      button: true,
      label: '${position.label}, $status',
      child: Tooltip(
        message: '${position.label} · $status',
        child: GestureDetector(
          key: Key('team-map-marker-${position.technicianId}'),
          onTap: onTap,
          child: Container(
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: color,
              shape: BoxShape.circle,
              border: Border.all(color: AppColors.panel, width: 3),
              boxShadow: const [
                BoxShadow(
                  color: Color(0x33000000),
                  blurRadius: 8,
                  offset: Offset(0, 3),
                ),
              ],
            ),
            child: Text(
              _initials(position.label),
              style: const TextStyle(
                color: Colors.white,
                fontSize: 12,
                fontWeight: FontWeight.w800,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _TechnicianTile extends StatelessWidget {
  const _TechnicianTile({
    required this.technician,
    required this.position,
    required this.onTap,
  });

  final ManagerTechnician technician;
  final ManagerTeamMapPosition? position;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final color = position == null
        ? technician.locationSharingEnabled
              ? AppColors.accent
              : AppColors.subdued(context)
        : _positionColor(context, position!);
    final status = position == null
        ? technician.locationSharingEnabled
              ? 'Waiting for location'
              : 'Not sharing'
        : _positionStatus(position!);
    final details = [
      technician.title,
      technician.region,
      technician.status.replaceAll('_', ' '),
      if (technician.activeWorkOrderTitle != null)
        technician.activeWorkOrderTitle,
    ].whereType<String>().where((value) => value.isNotEmpty).join(' · ');
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: LayoutBuilder(
        builder: (context, constraints) {
          final compact = constraints.maxWidth < 360;
          return ListTile(
            key: Key('team-technician-${technician.personId}'),
            leading: Icon(Icons.person_pin_circle_outlined, color: color),
            title: Text(technician.name),
            isThreeLine: compact,
            subtitle: compact
                ? Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        details,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                      const SizedBox(height: 4),
                      _StatusPill(label: status, color: color),
                    ],
                  )
                : Text(details, maxLines: 2, overflow: TextOverflow.ellipsis),
            trailing: compact ? null : _StatusPill(label: status, color: color),
            onTap: onTap,
          );
        },
      ),
    );
  }
}

class _TeamMapControls extends StatelessWidget {
  const _TeamMapControls({
    required this.filter,
    required this.total,
    required this.onQueryChanged,
    required this.onFilterChanged,
  });

  final _TeamFilter filter;
  final int? total;
  final ValueChanged<String> onQueryChanged;
  final ValueChanged<_TeamFilter> onFilterChanged;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        TextField(
          key: const Key('team-map-search'),
          onChanged: onQueryChanged,
          decoration: const InputDecoration(
            labelText: 'Search technicians',
            hintText: 'Name, region, title, or work order',
            prefixIcon: Icon(Icons.search),
          ),
        ),
        const SizedBox(height: 10),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              _TeamFilterChip(
                label: 'All',
                value: _TeamFilter.all,
                selected: filter,
                onChanged: onFilterChanged,
              ),
              _TeamFilterChip(
                label: 'Live',
                value: _TeamFilter.live,
                selected: filter,
                onChanged: onFilterChanged,
              ),
              _TeamFilterChip(
                label: 'Stale',
                value: _TeamFilter.stale,
                selected: filter,
                onChanged: onFilterChanged,
              ),
              _TeamFilterChip(
                label: 'Not sharing',
                value: _TeamFilter.notSharing,
                selected: filter,
                onChanged: onFilterChanged,
              ),
            ],
          ),
        ),
        const SizedBox(height: 14),
        Text(
          total == null ? 'Technicians' : 'Technicians · $total total',
          style: Theme.of(
            context,
          ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w800),
        ),
      ],
    );
  }
}

class _TeamFilterChip extends StatelessWidget {
  const _TeamFilterChip({
    required this.label,
    required this.value,
    required this.selected,
    required this.onChanged,
  });

  final String label;
  final _TeamFilter value;
  final _TeamFilter selected;
  final ValueChanged<_TeamFilter> onChanged;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: ChoiceChip(
        label: Text(label),
        selected: selected == value,
        showCheckmark: false,
        onSelected: (_) => onChanged(value),
      ),
    );
  }
}

class _TechnicianLocationSheet extends ConsumerWidget {
  const _TechnicianLocationSheet({
    required this.technician,
    required this.position,
    required this.onOpenDispatch,
  });

  final ManagerTechnician? technician;
  final ManagerTeamMapPosition? position;
  final VoidCallback? onOpenDispatch;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final title = technician?.name ?? position?.label ?? 'Technician';
    final locationDetail = position == null
        ? null
        : ref.watch(
            managerTechnicianLocationDetailProvider(position!.technicianId),
          );
    final displayedPosition = locationDetail?.valueOrNull?.position ?? position;
    final status = displayedPosition == null
        ? technician?.locationSharingEnabled == true
              ? 'Waiting for a shared location'
              : 'Location sharing is off'
        : _positionStatus(displayedPosition);
    return SafeArea(
      child: SingleChildScrollView(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                title,
                style: Theme.of(
                  context,
                ).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w800),
              ),
              const SizedBox(height: 6),
              Text(status),
              if (locationDetail != null) ...[
                const SizedBox(height: 14),
                Text(
                  displayedPosition!.isLive
                      ? 'Live location address'
                      : 'Last known address',
                  style: Theme.of(
                    context,
                  ).textTheme.labelLarge?.copyWith(fontWeight: FontWeight.w800),
                ),
                const SizedBox(height: 3),
                locationDetail.when(
                  loading: () => const Row(
                    children: [
                      SizedBox.square(
                        dimension: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                      SizedBox(width: 8),
                      Expanded(child: Text('Finding nearest address...')),
                    ],
                  ),
                  error: (_, _) => Row(
                    children: [
                      const Expanded(
                        child: Text('Address currently unavailable'),
                      ),
                      TextButton(
                        onPressed: () => ref.invalidate(
                          managerTechnicianLocationDetailProvider(
                            position!.technicianId,
                          ),
                        ),
                        child: const Text('Retry'),
                      ),
                    ],
                  ),
                  data: (detail) {
                    final address = detail.addressText?.trim();
                    if (detail.addressStatus !=
                            ManagerLocationAddressStatus.available ||
                        address == null ||
                        address.isEmpty) {
                      return const Text('Nearest address unavailable');
                    }
                    return Text(address);
                  },
                ),
              ],
              if (displayedPosition?.lastLocationAt != null) ...[
                const SizedBox(height: 10),
                Text(
                  'Location updated '
                  '${_relativeTime(displayedPosition!.lastLocationAt!)}',
                ),
              ],
              if (displayedPosition?.accuracyM != null) ...[
                const SizedBox(height: 4),
                Text('Accuracy ±${displayedPosition!.accuracyM!.round()} m'),
              ],
              if (technician?.activeWorkOrderTitle != null) ...[
                const SizedBox(height: 14),
                Text(
                  'Current work order',
                  style: Theme.of(
                    context,
                  ).textTheme.labelLarge?.copyWith(fontWeight: FontWeight.w800),
                ),
                const SizedBox(height: 3),
                Text(technician!.activeWorkOrderTitle!),
              ],
              if (onOpenDispatch != null) ...[
                const SizedBox(height: 16),
                FilledButton.icon(
                  onPressed: onOpenDispatch,
                  icon: const Icon(Icons.assignment_ind_outlined),
                  label: const Text('View dispatch'),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _TeamMapMessage extends StatelessWidget {
  const _TeamMapMessage({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: SizedBox(height: 330, child: Center(child: child)),
    );
  }
}

class _RetryMessage extends StatelessWidget {
  const _RetryMessage({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(message, textAlign: TextAlign.center),
          const SizedBox(height: 8),
          TextButton(onPressed: onRetry, child: const Text('Retry')),
        ],
      ),
    );
  }
}

class _InlineRefreshWarning extends StatelessWidget {
  const _InlineRefreshWarning({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        const Icon(Icons.cloud_off_outlined, size: 18),
        const SizedBox(width: 8),
        Expanded(child: Text(message)),
        TextButton(onPressed: onRetry, child: const Text('Retry')),
      ],
    );
  }
}

class _DispatchJobCard extends ConsumerWidget {
  const _DispatchJobCard({required this.job, required this.technicians});

  final ManagerJob job;
  final List<ManagerTechnician> technicians;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final canUnassign = job.assignmentQueueId != null;
    final time = job.scheduledStart == null
        ? 'Unscheduled'
        : DateFormat('d MMM, HH:mm').format(job.scheduledStart!.toLocal());
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                StatusPill(job.statusPresentation),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    time,
                    textAlign: TextAlign.right,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 10),
            Text(
              job.title,
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w800),
            ),
            const SizedBox(height: 6),
            Text(
              [job.workType, job.priority, job.subscriberLabel, job.addressText]
                  .whereType<String>()
                  .where((value) => value.isNotEmpty)
                  .join(' · '),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: AppColors.subdued(context),
              ),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: Text(
                    job.assignedToLabel == null
                        ? 'Unassigned'
                        : 'Assigned to ${job.assignedToLabel}',
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  onPressed: canUnassign
                      ? () => _unassign(context, ref, job)
                      : technicians.isEmpty
                      ? null
                      : () => _assign(context, ref, job, technicians),
                  icon: Icon(
                    canUnassign
                        ? Icons.person_remove_outlined
                        : Icons.assignment_ind_outlined,
                  ),
                  label: Text(canUnassign ? 'Unassign' : 'Assign'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _ExpenseApprovalCard extends ConsumerStatefulWidget {
  const _ExpenseApprovalCard({
    required this.request,
    required this.canPay,
    required this.onResolved,
  });

  final ExpenseRequest request;
  final bool canPay;
  final ValueChanged<String> onResolved;

  @override
  ConsumerState<_ExpenseApprovalCard> createState() =>
      _ExpenseApprovalCardState();
}

class _ExpenseApprovalCardState extends ConsumerState<_ExpenseApprovalCard> {
  bool _busy = false;

  Future<void> _approve() async {
    await _run(
      () async {
        final result = await ref
            .read(managerRepositoryProvider)
            .approveExpense(widget.request.id);
        return expenseApprovalMessage(result);
      },
      failureMessage:
          'Expense was not approved; ERP sync is unavailable. Please retry.',
    );
  }

  Future<void> _reject() async {
    final reason = await _rejectReason(context);
    if (reason == null || reason.trim().isEmpty) return;
    await _run(() async {
      await ref
          .read(managerRepositoryProvider)
          .rejectExpense(widget.request.id, reason);
      return 'Expense rejected';
    }, failureMessage: 'Could not reject expense');
  }

  Future<void> _pay() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Pay expense?'),
        content: Text(
          'This will initiate a bank transfer of '
          '${_money(widget.request.currency, widget.request.totalAmount)} '
          'through ERP and Paystack.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialogContext).pop(true),
            child: const Text('Pay expense'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    await _run(() async {
      final result = await ref
          .read(managerRepositoryProvider)
          .payExpense(widget.request.id);
      return result.paymentStatus == 'queued'
          ? 'Payment queued securely in ERP'
          : 'Payment status: ${result.paymentStatus}';
    }, failureMessage: 'Could not initiate payment. No retry was assumed.');
  }

  Future<void> _run(
    Future<String> Function() action, {
    required String failureMessage,
  }) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final message = await action();
      widget.onResolved(widget.request.id);
      ref
        ..invalidate(managerExpensesProvider)
        ..invalidate(managerSummaryProvider);
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(message)));
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(failureMessage)));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final request = widget.request;
    final paymentActive = const {
      'queued',
      'pending',
      'processing',
      'indeterminate',
    }.contains(request.paymentStatus);
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.receipt_long_outlined, color: AppColors.primary),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    request.purpose ?? request.displayNumber,
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
                Text(
                  _money(request.currency, request.totalAmount),
                  style: const TextStyle(fontWeight: FontWeight.w800),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              [
                request.displayNumber,
                request.workOrderId == null
                    ? null
                    : 'WO ${request.workOrderId}',
                '${request.items.length} item${request.items.length == 1 ? '' : 's'}',
              ].whereType<String>().join(' · '),
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: AppColors.subdued(context),
              ),
            ),
            const SizedBox(height: 4),
            Text(
              request.requestedByName == null
                  ? 'Raised by unavailable'
                  : 'Raised by ${request.requestedByName}',
              key: Key('expense-requester-${request.id}'),
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: AppColors.subdued(context),
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 12),
            if (request.paymentStatus != null) ...[
              Text(
                'Payment: ${request.paymentStatus!.replaceAll('_', ' ')}',
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
              if (request.paymentError != null)
                Text(
                  request.paymentError!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              const SizedBox(height: 10),
            ],
            if (request.status == 'submitted')
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: _busy ? null : _reject,
                      icon: const Icon(Icons.close),
                      label: const Text('Reject'),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: FilledButton.icon(
                      onPressed: _busy ? null : _approve,
                      icon: const Icon(Icons.check),
                      label: const Text('Approve'),
                    ),
                  ),
                ],
              )
            else if (request.status == 'approved' && widget.canPay)
              FilledButton.icon(
                onPressed: _busy || paymentActive ? null : _pay,
                icon: Icon(
                  paymentActive ? Icons.hourglass_top : Icons.payments_outlined,
                ),
                label: Text(
                  paymentActive ? 'Payment in progress' : 'Pay expense',
                ),
              )
            else
              Text(
                request.status.replaceAll('_', ' '),
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
          ],
        ),
      ),
    );
  }
}

class _ExpenseSectionTitle extends StatelessWidget {
  const _ExpenseSectionTitle({required this.title, required this.count});

  final String title;
  final int count;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: Text(
      '$title ($count)',
      style: Theme.of(
        context,
      ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w800),
    ),
  );
}

String expenseApprovalMessage(ExpenseApprovalResult result) {
  return switch (result.erpSyncStatus) {
    'pending' || 'sent' => 'Expense approved; waiting for ERP',
    'accepted' => 'Expense approved and synced to ERP',
    'rejected' ||
    'dead' ||
    'not_configured' ||
    'not_queued' => 'Expense approved; ERP sync needs attention',
    _ => 'Expense approved; waiting for ERP',
  };
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: color.withValues(alpha: 0.35)),
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        child: Text(
          label,
          style: Theme.of(context).textTheme.labelSmall?.copyWith(
            color: color,
            fontWeight: FontWeight.w800,
          ),
        ),
      ),
    );
  }
}

class _InlineError extends StatelessWidget {
  const _InlineError({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 32),
      child: Center(child: Text(message)),
    );
  }
}

Future<void> _assign(
  BuildContext context,
  WidgetRef ref,
  ManagerJob job,
  List<ManagerTechnician> technicians,
) async {
  final selected = await showModalBottomSheet<ManagerTechnician>(
    context: context,
    showDragHandle: true,
    builder: (context) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        children: [
          Text(
            'Assign technician',
            style: Theme.of(
              context,
            ).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w800),
          ),
          const SizedBox(height: 8),
          for (final tech in technicians)
            ListTile(
              leading: Icon(
                tech.isLive ? Icons.radio_button_checked : Icons.person_outline,
                color: tech.isLive ? AppColors.semanticPositive : null,
              ),
              title: Text(tech.name),
              subtitle: Text(
                [
                      tech.region,
                      tech.status.replaceAll('_', ' '),
                      tech.activeWorkOrderTitle,
                    ]
                    .whereType<String>()
                    .where((value) => value.isNotEmpty)
                    .join(' · '),
              ),
              onTap: () => Navigator.of(context).pop(tech),
            ),
        ],
      ),
    ),
  );
  if (selected == null) return;
  try {
    await ref
        .read(managerRepositoryProvider)
        .assignJob(jobId: job.id, personId: selected.personId);
    ref
      ..invalidate(managerJobsProvider)
      ..invalidate(managerSummaryProvider)
      ..invalidate(managerTechniciansProvider);
    if (context.mounted) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('Assigned to ${selected.name}')));
    }
  } catch (_) {
    if (context.mounted) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Could not assign job')));
    }
  }
}

Future<void> _unassign(
  BuildContext context,
  WidgetRef ref,
  ManagerJob job,
) async {
  final assignmentQueueId = job.assignmentQueueId;
  if (assignmentQueueId == null) return;
  final confirmed = await showDialog<bool>(
    context: context,
    builder: (context) => AlertDialog(
      title: const Text('Unassign technician?'),
      content: Text(
        'Remove ${job.assignedToLabel ?? 'the assigned technician'} from '
        '${job.title}?',
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text('Unassign'),
        ),
      ],
    ),
  );
  if (confirmed != true || !context.mounted) return;
  try {
    await ref
        .read(managerRepositoryProvider)
        .unassignJob(
          assignmentQueueId: assignmentQueueId,
          reason: 'Manager unassigned technician from mobile dispatch',
        );
    ref
      ..invalidate(managerJobsProvider)
      ..invalidate(managerSummaryProvider)
      ..invalidate(managerTechniciansProvider);
    if (context.mounted) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('Technician unassigned')));
    }
  } catch (_) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Could not unassign technician')),
      );
    }
  }
}

Future<String?> _rejectReason(BuildContext context) async {
  return showDialog<String>(
    context: context,
    builder: (_) => const _RejectExpenseDialog(),
  );
}

class _RejectExpenseDialog extends StatefulWidget {
  const _RejectExpenseDialog();

  @override
  State<_RejectExpenseDialog> createState() => _RejectExpenseDialogState();
}

class _RejectExpenseDialogState extends State<_RejectExpenseDialog> {
  final _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _submit() {
    FocusManager.instance.primaryFocus?.unfocus();
    Navigator.of(context).pop(_controller.text);
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Reject expense'),
      content: TextField(
        key: const Key('expense-rejection-reason'),
        controller: _controller,
        autofocus: true,
        maxLines: 3,
        textInputAction: TextInputAction.done,
        onSubmitted: (_) => _submit(),
        decoration: const InputDecoration(labelText: 'Reason'),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(onPressed: _submit, child: const Text('Reject')),
      ],
    );
  }
}

void _fitPositions(MapController controller, List<LatLng> points) {
  if (points.isEmpty) return;
  if (points.length == 1) {
    controller.move(points.single, 15);
    return;
  }
  controller.fitCamera(
    CameraFit.coordinates(
      coordinates: points,
      padding: const EdgeInsets.all(42),
      maxZoom: 15,
    ),
  );
}

Color _positionColor(BuildContext context, ManagerTeamMapPosition position) {
  if (!position.isLive) return AppColors.subdued(context);
  if (position.status == ManagerPresenceStatus.onBreak) {
    return AppColors.accent;
  }
  return AppColors.semanticPositive;
}

String _positionStatus(ManagerTeamMapPosition position) {
  final freshness = position.isLive ? 'Live' : 'Stale';
  return '$freshness · ${position.status.label}';
}

String _initials(String name) {
  final parts = name
      .trim()
      .split(RegExp(r'\s+'))
      .where((part) => part.isNotEmpty)
      .take(2)
      .toList();
  if (parts.isEmpty) return '?';
  return parts.map((part) => part[0].toUpperCase()).join();
}

String _relativeTime(DateTime timestamp) {
  final elapsed = DateTime.now().difference(timestamp.toLocal());
  if (elapsed.isNegative || elapsed.inSeconds < 30) return 'just now';
  if (elapsed.inMinutes < 1) return '${elapsed.inSeconds}s ago';
  if (elapsed.inHours < 1) return '${elapsed.inMinutes}m ago';
  if (elapsed.inDays < 1) return '${elapsed.inHours}h ago';
  return DateFormat('d MMM, HH:mm').format(timestamp.toLocal());
}

String _firstName(String name) {
  final trimmed = name.trim();
  if (trimmed.isEmpty) return 'Manager';
  return trimmed.split(RegExp(r'\s+')).first;
}

String _money(String? currency, double amount) {
  final code = (currency == null || currency.isEmpty) ? 'NGN' : currency;
  final symbol = '$code ';
  return '$symbol${NumberFormat.decimalPattern().format(amount)}';
}

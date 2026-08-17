import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';

import '../../app/theme.dart';
import '../../app/status_presentation.dart';
import '../../app/widgets/status_pill.dart';
import '../jobs/jobs_providers.dart';
import '../jobs/job_models.dart';
import '../jobs/widgets/job_card.dart';
import '../location/location_tracking_controller.dart';
import '../profile/profile_screen.dart';

const _filters = <(String?, String)>[
  (null, 'All'),
  ('dispatched', 'Assigned'),
  ('in_progress', 'Active'),
  ('completed', 'Done'),
];

class TodayScreen extends ConsumerWidget {
  const TodayScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final me = ref.watch(meProvider);
    final jobs = ref.watch(todayJobsProvider);
    final filter = ref.watch(jobsFilterProvider);
    final isDark = AppColors.dark(context);

    return Scaffold(
      body: DecoratedBox(
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: isDark
                ? [AppColors.surfaceDark, AppColors.groundDark]
                : [AppColors.surfaceLight, AppColors.groundLight],
          ),
        ),
        child: RefreshIndicator(
          onRefresh: () async {
            ref.invalidate(meProvider);
            ref.invalidate(todayJobsProvider);
          },
          child: CustomScrollView(
            physics: const AlwaysScrollableScrollPhysics(),
            slivers: [
              SliverPadding(
                padding: EdgeInsets.fromLTRB(
                  18,
                  MediaQuery.paddingOf(context).top + 18,
                  18,
                  8,
                ),
                sliver: SliverToBoxAdapter(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      me.when(
                        data: (data) => _TodayHeader(me: data),
                        loading: () => const _TodayHeaderSkeleton(),
                        error: (_, _) => const _TodayHeaderSkeleton(),
                      ),
                      const SizedBox(height: 12),
                      const SyncStatusBar(),
                      const SizedBox(height: 12),
                      const LocationSharingControls(),
                      if (jobs.value?.fromCache ?? false)
                        const _OfflineBanner(),
                      const SizedBox(height: 16),
                      _FilterRail(
                        selected: filter,
                        onSelected: (value) =>
                            ref.read(jobsFilterProvider.notifier).state = value,
                      ),
                      const SizedBox(height: 4),
                      Text(
                        'Open assigned work',
                        style: Theme.of(context).textTheme.titleMedium
                            ?.copyWith(fontWeight: FontWeight.w800),
                      ),
                      const SizedBox(height: 8),
                      Text(
                        'Today, overdue, and unscheduled jobs ready for action.',
                        style: Theme.of(context).textTheme.bodySmall?.copyWith(
                          color: AppColors.subdued(context),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              jobs.when(
                data: (list) => list.jobs.isEmpty
                    ? const SliverFillRemaining(
                        hasScrollBody: false,
                        child: Center(child: Text('No jobs in this view')),
                      )
                    : SliverPadding(
                        padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
                        sliver: SliverList.separated(
                          itemCount: list.jobs.length + 1,
                          separatorBuilder: (_, _) =>
                              const SizedBox(height: 12),
                          itemBuilder: (context, index) {
                            if (index == 0) {
                              final job = _preferredJob(list.jobs);
                              return _LiveJobPanel(
                                job: job,
                                onOpen: () => context.push('/jobs/${job.id}'),
                                onMap: () => context.go(
                                  '/map?jobId=${Uri.encodeQueryComponent(job.id)}',
                                ),
                              );
                            }
                            final job = list.jobs[index - 1];
                            return JobCard(
                              job: job,
                              onTap: () => context.push('/jobs/${job.id}'),
                            );
                          },
                        ),
                      ),
                loading: () => const SliverFillRemaining(
                  hasScrollBody: false,
                  child: Center(child: CircularProgressIndicator()),
                ),
                error: (error, _) => SliverFillRemaining(
                  hasScrollBody: false,
                  child: Center(
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(
                        'Could not load jobs — pull to retry',
                        textAlign: TextAlign.center,
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

String _firstName(String name) {
  final trimmed = name.trim();
  if (trimmed.isEmpty) return 'Technician';
  return trimmed.split(RegExp(r'\s+')).first;
}

JobSummary _preferredJob(List<JobSummary> jobs) {
  return jobs.firstWhere(
    (job) => job.status != 'completed' && job.status != 'canceled',
    orElse: () => jobs.first,
  );
}

class _TodayHeader extends StatelessWidget {
  const _TodayHeader({required this.me});

  final MeSummary me;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const _DotmacWordmark(),
            const Spacer(),
            Flexible(
              flex: 2,
              child: Text(
                'Hello, ${_firstName(me.name)}',
                textAlign: TextAlign.right,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.titleLarge?.copyWith(
                  fontWeight: FontWeight.w800,
                  color: AppColors.text(context),
                ),
              ),
            ),
            const SizedBox(width: 14),
            Icon(Icons.notifications_none, color: AppColors.text(context)),
          ],
        ),
        const SizedBox(height: 22),
        const _ShiftToggle(),
        const SizedBox(height: 18),
        _ConnectionStrip(
          openJobs: me.openJobs,
          completedToday: me.completedToday,
        ),
        const SizedBox(height: 16),
        Row(
          children: [
            _MetricTile(value: '${me.openJobs}', label: 'open'),
            const SizedBox(width: 10),
            _MetricTile(value: '${me.completedToday}', label: 'done today'),
            const SizedBox(width: 10),
            const _MetricTile(value: 'Live', label: 'status'),
          ],
        ),
      ],
    );
  }
}

class _TodayHeaderSkeleton extends StatelessWidget {
  const _TodayHeaderSkeleton();

  @override
  Widget build(BuildContext context) {
    return const Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            _DotmacWordmark(),
            Spacer(),
            SizedBox(width: 92, height: 24),
            SizedBox(width: 14),
            Icon(Icons.notifications_none, color: AppColors.ink),
          ],
        ),
        SizedBox(height: 22),
        _ShiftToggle(),
      ],
    );
  }
}

class _DotmacWordmark extends StatelessWidget {
  const _DotmacWordmark();

  @override
  Widget build(BuildContext context) {
    return RichText(
      text: TextSpan(
        text: 'DOTMAC',
        style: TextStyle(
          color: AppColors.semanticPositive,
          fontFamily: 'Georgia',
          fontSize: 25,
          fontWeight: FontWeight.w800,
        ),
        children: [
          TextSpan(
            text: '▪',
            style: TextStyle(color: AppColors.accent, fontSize: 18),
          ),
        ],
      ),
    );
  }
}

class _ShiftToggle extends StatelessWidget {
  const _ShiftToggle();

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 74,
      padding: const EdgeInsets.all(5),
      decoration: BoxDecoration(
        color: AppColors.dark(context)
            ? AppColors.surfaceDark
            : AppColors.groundLight,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        children: [
          Expanded(
            child: Container(
              height: double.infinity,
              decoration: BoxDecoration(
                color: AppColors.surface(context),
                borderRadius: BorderRadius.circular(16),
                boxShadow: [
                  BoxShadow(
                    color: Colors.black.withValues(alpha: 0.12),
                    blurRadius: 10,
                    offset: Offset(0, 4),
                  ),
                ],
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(Icons.circle, size: 18, color: AppColors.text(context)),
                  const SizedBox(width: 8),
                  Text(
                    'Active',
                    style: TextStyle(
                      color: AppColors.text(context),
                      fontSize: 18,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ],
              ),
            ),
          ),
          Expanded(
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: const [
                Icon(Icons.schedule, size: 18, color: AppColors.muted),
                SizedBox(width: 8),
                Text(
                  'Onboarding',
                  style: TextStyle(
                    color: AppColors.muted,
                    fontSize: 18,
                    fontWeight: FontWeight.w800,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _ConnectionStrip extends StatelessWidget {
  const _ConnectionStrip({
    required this.openJobs,
    required this.completedToday,
  });

  final int openJobs;
  final int completedToday;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 16),
      decoration: BoxDecoration(
        color: AppColors.softGreen(context),
        borderRadius: BorderRadius.circular(17),
      ),
      child: Row(
        children: [
          Container(
            width: 22,
            height: 22,
            decoration: BoxDecoration(
              color: AppColors.semanticPositive,
              borderRadius: BorderRadius.circular(999),
              boxShadow: [
                BoxShadow(
                  color: AppColors.semanticPositive.withValues(alpha: 0.14),
                  spreadRadius: 8,
                  blurRadius: 0,
                ),
              ],
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              'Connected · $openJobs open · $completedToday done',
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: AppColors.semanticPositive,
                fontSize: 19,
                fontWeight: FontWeight.w800,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _MetricTile extends StatelessWidget {
  const _MetricTile({required this.value, required this.label});

  final String value;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 16),
        decoration: BoxDecoration(
          color: AppColors.surface(context),
          border: Border.all(color: AppColors.border(context), width: 1.5),
          borderRadius: BorderRadius.circular(18),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              value,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(context).textTheme.titleLarge?.copyWith(
                color: value == 'Live'
                    ? AppColors.semanticPositive
                    : AppColors.text(context),
                fontWeight: FontWeight.w900,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              label,
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

class _FilterRail extends StatelessWidget {
  const _FilterRail({required this.selected, required this.onSelected});

  final String? selected;
  final ValueChanged<String?> onSelected;

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: Row(
        children: [
          for (final (value, label) in _filters) ...[
            Padding(
              padding: const EdgeInsets.only(right: 8),
              child: ChoiceChip(
                label: Text(label),
                selected: selected == value,
                showCheckmark: false,
                onSelected: (_) => onSelected(value),
                labelStyle: TextStyle(
                  color: selected == value
                      ? AppColors.text(context)
                      : AppColors.subdued(context),
                  fontWeight: FontWeight.w800,
                ),
                side: BorderSide(
                  color: selected == value
                      ? AppColors.surface(context)
                      : AppColors.border(context),
                ),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(15),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _LiveJobPanel extends StatelessWidget {
  const _LiveJobPanel({
    required this.job,
    required this.onOpen,
    required this.onMap,
  });

  final JobSummary job;
  final VoidCallback onOpen;
  final VoidCallback onMap;

  @override
  Widget build(BuildContext context) {
    final time = job.scheduledStart != null
        ? DateFormat.Hm().format(job.scheduledStart!.toLocal())
        : 'Any time';
    return Container(
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        color: AppColors.surface(context),
        border: Border.all(color: AppColors.primary, width: 2),
        borderRadius: BorderRadius.circular(AppRadii.feature),
        boxShadow: [
          BoxShadow(
            color: AppColors.accent.withValues(alpha: 0.10),
            blurRadius: 34,
            offset: Offset(0, 16),
          ),
        ],
      ),
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(22, 22, 22, 16),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.only(top: 7),
                  child: Icon(
                    Icons.engineering_outlined,
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(width: 14),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Next service job',
                        style: Theme.of(context).textTheme.titleLarge?.copyWith(
                          fontWeight: FontWeight.w900,
                          color: AppColors.text(context),
                        ),
                      ),
                      const SizedBox(height: 5),
                      Text(
                        '$time · ${job.title}',
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: Theme.of(context).textTheme.bodyLarge?.copyWith(
                          color: AppColors.subdued(context),
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 10),
                StatusPill(job.statusPresentation),
              ],
            ),
          ),
          _RoutePreview(surfaceColor: AppColors.surface(context)),
          Padding(
            padding: const EdgeInsets.fromLTRB(18, 16, 18, 18),
            child: Row(
              children: [
                Expanded(
                  flex: 3,
                  child: FilledButton.icon(
                    onPressed: onOpen,
                    icon: const Icon(Icons.play_arrow_rounded),
                    label: const Text('Open job'),
                    style: FilledButton.styleFrom(
                      backgroundColor: AppColors.primary,
                      foregroundColor: Colors.white,
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  flex: 2,
                  child: OutlinedButton.icon(
                    onPressed: onMap,
                    icon: const Icon(Icons.map_outlined),
                    label: const Text('Map'),
                    style: OutlinedButton.styleFrom(
                      foregroundColor: AppColors.text(context),
                      side: BorderSide(
                        color: AppColors.border(context),
                        width: 1.5,
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _RoutePreview extends StatelessWidget {
  const _RoutePreview({required this.surfaceColor});

  final Color surfaceColor;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 156,
      width: double.infinity,
      child: CustomPaint(painter: _RoutePreviewPainter(surfaceColor)),
    );
  }
}

class _RoutePreviewPainter extends CustomPainter {
  const _RoutePreviewPainter(this.surfaceColor);

  final Color surfaceColor;

  @override
  void paint(Canvas canvas, Size size) {
    final grid = Paint()
      ..color = AppColors.semanticNeutral.withValues(alpha: 0.16)
      ..strokeWidth = 1;
    for (double x = 0; x <= size.width; x += 38) {
      canvas.drawLine(Offset(x, 0), Offset(x, size.height), grid);
    }
    for (double y = 0; y <= size.height; y += 38) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), grid);
    }

    final route = Path()
      ..moveTo(size.width * 0.09, size.height * 0.72)
      ..cubicTo(
        size.width * 0.25,
        size.height * 0.62,
        size.width * 0.34,
        size.height * 0.55,
        size.width * 0.48,
        size.height * 0.60,
      )
      ..cubicTo(
        size.width * 0.62,
        size.height * 0.65,
        size.width * 0.62,
        size.height * 0.32,
        size.width * 0.78,
        size.height * 0.38,
      )
      ..cubicTo(
        size.width * 0.85,
        size.height * 0.42,
        size.width * 0.91,
        size.height * 0.32,
        size.width * 0.95,
        size.height * 0.27,
      );
    canvas.drawPath(
      route,
      Paint()
        ..color = AppColors.primary
        ..strokeWidth = 5
        ..style = PaintingStyle.stroke
        ..strokeCap = StrokeCap.round,
    );

    void point(Offset center, Color color, double halo) {
      canvas.drawCircle(
        center,
        17,
        Paint()..color = color.withValues(alpha: halo),
      );
      canvas.drawCircle(center, 12, Paint()..color = surfaceColor);
      canvas.drawCircle(center, 8, Paint()..color = color);
    }

    point(
      Offset(size.width * 0.35, size.height * 0.55),
      AppColors.primary,
      0.18,
    );
    point(
      Offset(size.width * 0.95, size.height * 0.27),
      AppColors.semanticPositive,
      0.16,
    );
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}

/// Compact sync state: queued work + items needing review, tap → Profile.
/// Calm by design — amber for attention, never red.
class SyncStatusBar extends ConsumerWidget {
  const SyncStatusBar({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final pending = ref.watch(pendingOutboxProvider).value?.length ?? 0;
    final photos = ref.watch(pendingPhotosProvider).value ?? 0;
    final conflicts = ref.watch(conflictOutboxProvider).value?.length ?? 0;
    final queued = pending + photos;
    if (queued == 0 && conflicts == 0) return const SizedBox.shrink();

    final warning = AppColors.statusTone(context, StatusTone.warning);
    final parts = <String>[
      if (queued > 0) '$queued queued',
      if (conflicts > 0) '$conflicts need review',
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: InkWell(
        key: const Key('sync-status-bar'),
        onTap: () => context.go('/profile'),
        borderRadius: BorderRadius.circular(8),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: warning.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(8),
          ),
          child: Row(
            children: [
              Icon(Icons.sync, size: 16, color: warning),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  parts.join(' · '),
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
              const Icon(Icons.chevron_right, size: 16),
            ],
          ),
        ),
      ),
    );
  }
}

class _OfflineBanner extends StatelessWidget {
  const _OfflineBanner();

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Row(
        key: const Key('offline-banner'),
        children: [
          const Icon(Icons.cloud_off_outlined, size: 16),
          const SizedBox(width: 8),
          Text(
            'Offline — showing saved jobs',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}

import 'package:dotmac_field/app/status_presentation.dart';
import 'package:dotmac_field/app/theme.dart';
import 'package:dotmac_field/features/execution/completion_wizard.dart';
import 'package:dotmac_field/features/execution/signature_pad.dart';
import 'package:dotmac_field/features/jobs/job_chat_screen.dart';
import 'package:dotmac_field/features/jobs/job_detail_screen.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/jobs/widgets/job_card.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

JobSummary _job({String status = 'dispatched', String workType = 'install'}) =>
    JobSummary(
      id: 'wo-1',
      title: 'Install fiber — Adaeze Okafor',
      status: status,
      workType: workType,
      priority: 'normal',
      scheduledStart: DateTime.utc(2026, 6, 10, 9),
      estimatedDurationMinutes: 90,
      statusPresentation: StatusPresentation(
        value: status,
        label: status == 'dispatched'
            ? 'Dispatched'
            : status.replaceAll('_', ' '),
        tone: status == 'completed' ? StatusTone.positive : StatusTone.info,
        icon: status == 'completed' ? 'check' : 'info',
      ),
    );

JobDetail _detail({
  String status = 'dispatched',
  JobLocation? location,
  List<Map<String, dynamic>> materialRequests = const [],
  List<Map<String, dynamic>> history = const [],
  String? projectId,
  String? accessNotes,
  List<JobSiteContact> additionalContacts = const [],
  List<JobVisitHistoryItem> recentVisits = const [],
  List<JobOpenTicketItem> openTickets = const [],
}) => JobDetail(
  job: _job(status: status),
  location:
      location ??
      const JobLocation(
        latitude: 6.43,
        longitude: 3.42,
        addressText: '12 Admiralty Way',
        source: 'geocoded',
      ),
  customer: const JobCustomer(
    name: 'Adaeze Okafor',
    phone: '+2348012345678',
    email: 'adaeze@example.com',
    servicePlan: '100 Mbps',
    accountNumber: 'ACCT-1001',
    status: 'active',
  ),
  customerExperience: JobCustomerExperience(
    project: projectId == null
        ? null
        : JobLifecycleReference(
            id: projectId,
            title: 'Fiber installation',
            status: 'active',
          ),
    originTicket: const JobLifecycleReference(
      id: 'ticket-1',
      number: 'TCK-1001',
      title: 'Connectivity incident',
      status: 'open',
    ),
  ),
  accessNotes: accessNotes,
  additionalContacts: additionalContacts,
  recentVisits: recentVisits,
  openTickets: openTickets,
  materialRequests: materialRequests,
  history: history,
);

Widget _wrap(
  Widget child, {
  List<Override> overrides = const [],
  List<JobDestination> destinations = const [],
}) => ProviderScope(
  overrides: [
    jobDestinationsProvider('wo-1').overrideWith((ref) async => destinations),
    ...overrides,
  ],
  child: MaterialApp(theme: lightTheme, home: child),
);

void main() {
  test('job summary accepts nullable migrated classification fields', () {
    final job = JobSummary.fromJson({
      'id': 'WO-1',
      'title': 'Migrated repair',
      'status': 'dispatched',
      'work_type': null,
      'priority': null,
      'estimated_duration_minutes': '45',
      'total_active_seconds': 90.0,
    });

    expect(job.workType, 'other');
    expect(job.priority, 'normal');
    expect(job.estimatedDurationMinutes, 45);
    expect(job.totalActiveSeconds, 90);
  });

  test('today includes overdue and unscheduled open work', () {
    final day = DateTime(2026, 8, 17);
    JobSummary job(String id, {DateTime? scheduledStart}) => JobSummary(
      id: id,
      title: id,
      status: 'dispatched',
      workType: 'other',
      priority: 'normal',
      scheduledStart: scheduledStart,
    );

    expect(isActionableOnDay(job('unscheduled'), day), isTrue);
    expect(
      isActionableOnDay(
        job('overdue', scheduledStart: DateTime(2026, 8, 16, 12)),
        day,
      ),
      isTrue,
    );
    expect(
      isActionableOnDay(
        job('future', scheduledStart: DateTime(2026, 8, 18, 12)),
        day,
      ),
      isFalse,
    );
  });

  testWidgets('job card shows status stripe, pill, and meta', (tester) async {
    await tester.pumpWidget(_wrap(JobCard(job: _job())));

    expect(find.text('Install'), findsOneWidget);
    expect(find.text('Install fiber — Adaeze Okafor'), findsOneWidget);
    expect(find.text('DISPATCHED'), findsOneWidget);
    expect(find.text('~90 min'), findsOneWidget);

    final stripe = tester
        .widgetList<Container>(find.byType(Container))
        .firstWhere(
          (c) => c.color == AppColors.semanticInfo,
          orElse: () => tester.widget<Container>(find.byType(Container).first),
        );
    expect(stripe.color, AppColors.semanticInfo);
  });

  group('action bar shows work actions per status', () {
    for (final (status, expected) in [
      ('scheduled', ['En Route']),
      ('dispatched', ['En Route']),
      ('in_progress', ['Pause Work', 'Complete Work']),
      ('paused', ['Resume Work']),
    ]) {
      testWidgets(status, (tester) async {
        final detail = _detail(status: status);
        await tester.pumpWidget(
          _wrap(
            const SizedBox(),
            overrides: [
              jobDetailProvider('wo-1').overrideWith((ref) async => detail),
            ],
          ),
        );
        await tester.pumpWidget(
          _wrap(
            const JobDetailScreen(jobId: 'wo-1'),
            overrides: [
              jobDetailProvider('wo-1').overrideWith((ref) async => detail),
            ],
          ),
        );
        await tester.pumpAndSettle();

        for (final label in expected) {
          expect(find.text(label), findsOneWidget);
        }
      });
    }

    testWidgets('departure unlocks arrival', (tester) async {
      final detail = _detail(
        status: 'dispatched',
        history: const [
          {'event': 'en_route'},
        ],
      );
      await tester.pumpWidget(
        _wrap(
          const JobDetailScreen(jobId: 'wo-1'),
          overrides: [
            jobDetailProvider('wo-1').overrideWith((ref) async => detail),
          ],
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Arrived'), findsOneWidget);
    });

    testWidgets('arrival unlocks start work', (tester) async {
      final detail = _detail(
        status: 'dispatched',
        history: const [
          {'event': 'en_route'},
          {'event': 'arrived'},
        ],
      );
      await tester.pumpWidget(
        _wrap(
          const JobDetailScreen(jobId: 'wo-1'),
          overrides: [
            jobDetailProvider('wo-1').overrideWith((ref) async => detail),
          ],
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Start Work'), findsOneWidget);
    });

    testWidgets('completed jobs have no work action', (tester) async {
      final detail = _detail(status: 'completed');
      await tester.pumpWidget(
        _wrap(
          const JobDetailScreen(jobId: 'wo-1'),
          overrides: [
            jobDetailProvider('wo-1').overrideWith((ref) async => detail),
          ],
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('work-action-start')), findsNothing);
      expect(find.byKey(const Key('work-action-pause')), findsNothing);
      expect(find.byKey(const Key('work-action-resume')), findsNothing);
      expect(find.byKey(const Key('work-action-complete')), findsNothing);
    });
  });

  testWidgets('navigate button launches directions uri from coordinates', (
    tester,
  ) async {
    final launched = <Uri>[];
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => _detail()),
          uriLauncherProvider.overrideWithValue((uri) async {
            launched.add(uri);
            return true;
          }),
        ],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('navigate-button')));
    expect(launched.single.scheme, 'https');
    expect(launched.single.host, 'www.google.com');
    expect(launched.single.path, '/maps/dir/');
    expect(launched.single.queryParameters['destination'], '6.43,3.42');
  });

  testWidgets('address-only location falls back to maps text search', (
    tester,
  ) async {
    final launched = <Uri>[];
    const location = JobLocation(
      addressText: '12 Admiralty Way, Lekki',
      source: 'address_only',
    );
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider(
            'wo-1',
          ).overrideWith((ref) async => _detail(location: location)),
          uriLauncherProvider.overrideWithValue((uri) async {
            launched.add(uri);
            return true;
          }),
        ],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('navigate-button')));
    expect(launched.single.scheme, 'https');
    expect(launched.single.host, 'www.google.com');
    expect(launched.single.path, '/maps/search/');
    expect(launched.single.queryParameters['query'], '12 Admiralty Way, Lekki');
  });

  test('maps uri is null when nothing is known', () {
    const location = JobLocation(source: 'none');
    expect(location.mapsUri, isNull);
  });

  test('job detail accepts paginated notes and ignores malformed entries', () {
    final detail = JobDetail.fromJson({
      'job': {
        'id': 'wo-1',
        'title': 'Install',
        'status': 'dispatched',
        'work_type': 'install',
        'priority': 'normal',
      },
      'location': {'source': 'none'},
      'notes': {
        'items': [
          {'text': 'Stored note returned as text'},
          null,
          'unexpected',
        ],
      },
      'history': {
        'items': [
          {'type': 'note', 'title': 'History note'},
        ],
      },
      'materials': null,
    });

    expect(detail.notes, hasLength(1));
    expect(detail.notes.single['text'], 'Stored note returned as text');
    expect(detail.history, hasLength(1));
    expect(detail.history.single['title'], 'History note');
  });

  test('job detail parses field-service lifecycle context', () {
    final detail = JobDetail.fromJson({
      'job': {
        'id': 'wo-1',
        'title': 'Repair outage',
        'status': 'dispatched',
        'work_type': 'repair',
        'priority': 'high',
      },
      'location': {'source': 'none'},
      'completion_requirements': {
        'evidence_required': false,
        'minimum_photo_count': 0,
        'customer_signoff_required': false,
        'signature_unavailable_reason_allowed': false,
      },
      'customer': {
        'subscriber_id': 'sub-1',
        'name': 'Adaeze Okafor',
        'phone': '+2348012345678',
        'email': 'adaeze@example.com',
        'address_text': '12 Admiralty Way',
        'service_plan': '100 Mbps',
        'account_number': 'ACCT-1001',
        'status': 'active',
      },
      'customer_experience': {
        'project': {
          'id': 'project-1',
          'name': 'Fiber installation',
          'status': 'active',
        },
        'origin_ticket': {
          'id': 'ticket-1',
          'number': 'TCK-1001',
          'title': 'Connectivity incident',
          'status': 'open',
        },
      },
      'access_notes': 'Ask estate security for rack room key.',
      'additional_contacts': [
        {
          'name': 'Facilities Desk',
          'phone': '+2348099999999',
          'email': 'facilities@example.com',
          'relationship': 'active',
        },
      ],
      'open_tickets': [
        {
          'id': 'ticket-2',
          'ref': 'TCK-1002',
          'subject': 'Slow speeds',
          'status': 'open',
        },
      ],
      'recent_visits': [
        {
          'work_order_id': 'wo-old',
          'title': 'Signal check',
          'work_type': 'maintenance',
          'status': 'completed',
          'completed_at': '2026-06-09T10:00:00Z',
        },
      ],
    });

    expect(detail.customer?.email, 'adaeze@example.com');
    expect(detail.completionRequirements.evidenceRequired, isFalse);
    expect(detail.completionRequirements.minimumPhotoCount, 0);
    expect(detail.customerExperience.project?.id, 'project-1');
    expect(detail.customerExperience.originTicket?.number, 'TCK-1001');
    expect(detail.accessNotes, contains('rack room key'));
    expect(detail.additionalContacts.single.name, 'Facilities Desk');
    expect(detail.openTickets.single.ref, 'TCK-1002');
    expect(detail.recentVisits.single.workType, 'maintenance');
  });

  test('job detail uses a strict fallback when cached policy is absent', () {
    final detail = JobDetail.fromJson({
      'job': {
        'id': 'wo-1',
        'title': 'Repair outage',
        'status': 'in_progress',
        'work_type': 'repair',
        'priority': 'high',
      },
      'location': {'source': 'none'},
    });

    expect(detail.completionRequirements.evidenceRequired, isTrue);
    expect(detail.completionRequirements.minimumPhotoCount, 1);
    expect(detail.completionRequirements.customerSignoffRequired, isTrue);
  });

  testWidgets('wizard permits completion when server evidence policy is off', (
    tester,
  ) async {
    const requirements = JobCompletionRequirements(
      evidenceRequired: false,
      minimumPhotoCount: 0,
      customerSignoffRequired: false,
      signatureUnavailableReasonAllowed: false,
    );
    await tester.pumpWidget(
      _wrap(const CompletionWizard(jobId: 'wo-1', requirements: requirements)),
    );

    expect(find.byKey(const Key('completion-prerequisites')), findsOneWidget);
    expect(find.text('Work photos are optional'), findsOneWidget);
    expect(find.text('Customer sign-off is optional'), findsOneWidget);

    await tester.tap(find.byKey(const Key('wizard-next')));
    await tester.pump();
    expect(find.text('Photos: 0 · optional'), findsOneWidget);
    await tester.tap(find.byKey(const Key('wizard-next')));
    await tester.pump();

    expect(find.text('Customer sign-off (optional)'), findsOneWidget);
    expect(find.byKey(const Key('equipment-serial')), findsNothing);
    final finish = tester.widget<FilledButton>(
      find.byKey(const Key('wizard-finish')),
    );
    expect(finish.onPressed, isNotNull);
  });

  testWidgets('signature can be cleared and redrawn', (tester) async {
    const requirements = JobCompletionRequirements(
      evidenceRequired: false,
      minimumPhotoCount: 0,
      customerSignoffRequired: true,
      signatureUnavailableReasonAllowed: false,
    );
    await tester.pumpWidget(
      _wrap(const CompletionWizard(jobId: 'wo-1', requirements: requirements)),
    );
    await tester.tap(find.byKey(const Key('wizard-next')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('wizard-next')));
    await tester.pump();

    final pad = find.byType(SignaturePad);
    final detector = tester.widget<GestureDetector>(
      find.descendant(of: pad, matching: find.byType(GestureDetector)),
    );
    detector.onPanStart!(
      DragStartDetails(
        globalPosition: const Offset(20, 20),
        localPosition: const Offset(20, 20),
      ),
    );
    detector.onPanUpdate!(
      DragUpdateDetails(
        globalPosition: const Offset(100, 40),
        localPosition: const Offset(100, 40),
        delta: const Offset(80, 20),
      ),
    );
    await tester.pump();
    final clear = find.byKey(const Key('clear-signature'));
    expect(tester.widget<TextButton>(clear).onPressed, isNotNull);

    await tester.tap(clear);
    await tester.pump();
    expect(tester.widget<TextButton>(clear).onPressed, isNull);
  });

  test('job chat thread parses field-service messages', () {
    final thread = JobChatThread.fromJson({
      'available': true,
      'can_send': true,
      'conversation_id': 'conv-1',
      'customer_name': 'Adaeze Okafor',
      'messages': [
        {
          'id': 'msg-1',
          'body': 'I am at home',
          'direction': 'customer',
          'created_at': '2026-06-10T09:00:00Z',
        },
      ],
    });

    expect(thread.available, isTrue);
    expect(thread.canSend, isTrue);
    expect(thread.conversationId, 'conv-1');
    expect(thread.messages.single.isCustomer, isTrue);
    expect(thread.messages.single.body, 'I am at home');
  });

  testWidgets('job detail renders notes returned with alternate body key', (
    tester,
  ) async {
    final detail = JobDetail(
      job: _job(),
      location: const JobLocation(source: 'none'),
      notes: const [
        {
          'text': 'Stored note returned as text',
          'author_name': 'Adaeze Okafor',
        },
      ],
    );
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => detail),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Stored note returned as text'), findsOneWidget);
    expect(find.text('Adaeze Okafor'), findsOneWidget);
  });

  testWidgets('call button dials the customer', (tester) async {
    final launched = <Uri>[];
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => _detail()),
          uriLauncherProvider.overrideWithValue((uri) async {
            launched.add(uri);
            return true;
          }),
        ],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('call-button')));
    expect(launched.single.scheme, 'tel');
  });

  testWidgets('job chat screen renders field-service thread', (tester) async {
    await tester.pumpWidget(
      _wrap(
        const JobChatScreen(jobId: 'wo-1'),
        overrides: [
          jobChatProvider('wo-1').overrideWith(
            (ref) async => JobChatThread(
              available: true,
              canSend: true,
              conversationId: 'conv-1',
              customerName: 'Adaeze Okafor',
              messages: [
                JobChatMessage(
                  id: 'msg-1',
                  body: 'I am at home',
                  direction: 'customer',
                  createdAt: DateTime.utc(2026, 6, 10, 9),
                ),
                JobChatMessage(
                  id: 'msg-2',
                  body: 'I am on my way',
                  direction: 'staff',
                  authorName: 'Technician',
                  createdAt: DateTime.utc(2026, 6, 10, 9, 5),
                ),
              ],
            ),
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Technician chat'), findsOneWidget);
    expect(find.text('I am at home'), findsOneWidget);
    expect(find.text('I am on my way'), findsOneWidget);
    expect(find.byKey(const Key('job-chat-input')), findsOneWidget);
    expect(find.byKey(const Key('job-chat-send')), findsOneWidget);
  });

  testWidgets('job chat screen explains when technician chat is unavailable', (
    tester,
  ) async {
    await tester.pumpWidget(
      _wrap(
        const JobChatScreen(jobId: 'wo-1'),
        overrides: [
          jobChatProvider(
            'wo-1',
          ).overrideWith((ref) async => const JobChatThread(available: false)),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Technician chat unavailable'), findsOneWidget);
  });

  testWidgets(
    'job detail renders lifecycle context and destination directions',
    (tester) async {
      final launched = <Uri>[];
      final detail = _detail(
        projectId: '12345678-90ab-cdef-1234-567890abcdef',
        accessNotes: 'Ask estate security for rack room key.',
        additionalContacts: const [
          JobSiteContact(
            name: 'Facilities Desk',
            phone: '+2348099999999',
            email: 'facilities@example.com',
            relationship: 'active',
          ),
        ],
        openTickets: const [
          JobOpenTicketItem(
            id: 'ticket-2',
            ref: 'TCK-1002',
            subject: 'Slow speeds',
            status: 'open',
          ),
        ],
        recentVisits: [
          JobVisitHistoryItem(
            workOrderId: 'wo-old',
            title: 'Signal check',
            workType: 'maintenance',
            status: 'completed',
            completedAt: DateTime.utc(2026, 6, 9, 10),
          ),
        ],
      );

      await tester.pumpWidget(
        _wrap(
          const JobDetailScreen(jobId: 'wo-1'),
          destinations: const [
            JobDestination(
              destinationType: 'customer',
              label: 'Customer site',
              latitude: 6.43,
              longitude: 3.42,
              addressText: '12 Admiralty Way',
            ),
            JobDestination(
              destinationType: 'pop',
              label: 'POP Lekki-01',
              latitude: 6.44,
              longitude: 3.43,
            ),
          ],
          overrides: [
            jobDetailProvider('wo-1').overrideWith((ref) async => detail),
            uriLauncherProvider.overrideWithValue((uri) async {
              launched.add(uri);
              return true;
            }),
          ],
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Ticket TCK-1001'), findsOneWidget);
      expect(find.textContaining('Project Fiber installation'), findsOneWidget);
      expect(find.text('Navigation targets'), findsOneWidget);
      expect(find.text('Customer site'), findsOneWidget);
      expect(find.text('POP Lekki-01'), findsOneWidget);
      expect(find.byKey(const Key('open-fiber-evidence-map')), findsOneWidget);
      expect(find.text('Open evidence map'), findsOneWidget);
      expect(find.text('Job context'), findsOneWidget);
      expect(
        find.text('Ask estate security for rack room key.'),
        findsOneWidget,
      );
      expect(find.text('Facilities Desk'), findsOneWidget);
      expect(find.text('Slow speeds'), findsOneWidget);
      expect(find.text('Signal check'), findsOneWidget);

      await tester.drag(
        find.byType(SingleChildScrollView),
        const Offset(0, -160),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('navigate-destination-pop')));
      expect(launched.single.scheme, 'https');
      expect(launched.single.host, 'www.google.com');
      expect(launched.single.path, '/maps/dir/');
      expect(launched.single.queryParameters['destination'], '6.44,3.43');
    },
  );

  testWidgets('job detail app bar offers material and expense requests', (
    tester,
  ) async {
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => _detail()),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byTooltip('Request materials'), findsOneWidget);
    expect(find.byTooltip('Request expense'), findsOneWidget);
  });

  testWidgets('job detail shows linked material requests', (tester) async {
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith(
            (ref) async => _detail(
              materialRequests: const [
                {
                  'id': 'mr-1',
                  'number': 'MR-1001',
                  'status': 'submitted',
                  'items': [
                    {'id': 'item-1'},
                  ],
                },
              ],
            ),
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Material requests'), findsOneWidget);
    expect(find.text('MR-1001'), findsOneWidget);
    expect(find.text('submitted · 1 item'), findsOneWidget);
  });

  testWidgets('job detail shows combined history activity', (tester) async {
    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith(
            (ref) async => _detail(
              history: const [
                {
                  'id': 'mr:1',
                  'type': 'material_request',
                  'title': 'Material request MR-1001',
                  'description': 'submitted · 1 item',
                  'occurred_at': '2026-06-10T09:00:00Z',
                  'status': 'submitted',
                },
                {
                  'id': 'note:1',
                  'type': 'note',
                  'title': 'Internal note',
                  'description': 'Checked signal at cabinet',
                  'occurred_at': '2026-06-10T09:05:00Z',
                  'actor_name': 'Adaeze Okafor',
                  'is_internal': true,
                },
                {
                  'id': 'event:1',
                  'type': 'work_event',
                  'title': 'Work started',
                  'occurred_at': '2026-06-10T09:10:00Z',
                  'status': 'start',
                },
              ],
            ),
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('History'), findsOneWidget);
    expect(find.text('Material request MR-1001'), findsOneWidget);
    expect(find.text('submitted · 1 item'), findsOneWidget);
    expect(find.text('Checked signal at cabinet'), findsOneWidget);
    expect(find.text('Internal'), findsOneWidget);
    expect(find.text('Work started'), findsOneWidget);
  });

  testWidgets('technician can open add note composer from job detail', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      _wrap(
        const JobDetailScreen(jobId: 'wo-1'),
        overrides: [
          jobDetailProvider('wo-1').overrideWith((ref) async => _detail()),
        ],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('add-note-action')));
    await tester.pumpAndSettle();

    expect(find.text('Add note'), findsWidgets);
    expect(find.byKey(const Key('note-body-field')), findsOneWidget);
    expect(find.byKey(const Key('internal-note-checkbox')), findsOneWidget);
    expect(find.text('Visible to staff only'), findsOneWidget);
    expect(find.byKey(const Key('save-note-action')), findsOneWidget);
    expect(
      tester.getTopLeft(find.byKey(const Key('note-body-field'))).dy,
      greaterThanOrEqualTo(0),
    );
    expect(
      tester.getBottomRight(find.byKey(const Key('note-body-field'))).dy,
      lessThan(tester.view.physicalSize.height),
    );
  });
}

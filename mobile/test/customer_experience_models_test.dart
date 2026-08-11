import 'package:dotmac_portal/src/models/project.dart';
import 'package:dotmac_portal/src/models/ticket.dart';
import 'package:dotmac_portal/src/models/work_order.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('work-order actions and status presentation are server-owned', () {
    final item = WorkOrderItem.fromJson({
      'id': '83ed1a57-35de-4df5-8278-67adfae4f90d',
      'public_id': 'WO-1001',
      'title': 'Restore fiber service',
      'status': 'in_progress',
      'status_presentation': {
        'value': 'in_progress',
        'label': 'Technician working',
        'tone': 'info',
        'icon': 'clock',
      },
      'actions': [
        {'key': 'view_work_order', 'allowed': true},
        {'key': 'track_technician', 'allowed': true},
      ],
    });

    expect(item.statusPresentation.label, 'Technician working');
    expect(item.canTrackTechnician, isTrue);
    expect(item.canRateTechnician, isFalse);
  });

  test('project parses the native lifecycle presentation', () {
    final item = ProjectItem.fromJson({
      'id': 'd17aa8c5-57ce-44fb-a61a-43dc2a34181c',
      'name': 'Fiber installation',
      'status': 'active',
      'experience_state': 'field_work',
      'progress_pct': 50,
      'status_presentation': {
        'value': 'active',
        'label': 'Installation active',
        'tone': 'info',
        'icon': 'clock',
      },
      'stages': [],
    });

    expect(item.experienceState, 'field_work');
    expect(item.statusPresentation.label, 'Installation active');
  });

  test('ticket rating availability comes from the server action projection',
      () {
    final withoutAction = Ticket.fromJson({
      'id': 'ticket-1',
      'title': 'Connectivity incident',
      'status': 'closed',
      'priority': 'normal',
      'resolution_actions': [],
    });
    final withAction = Ticket.fromJson({
      'id': 'ticket-2',
      'title': 'Connectivity incident',
      'status': 'closed',
      'priority': 'normal',
      'resolution_actions': [
        {'key': 'rate_support', 'allowed': true},
      ],
    });

    expect(withoutAction.canRate, isFalse);
    expect(withAction.canRate, isTrue);
  });
}

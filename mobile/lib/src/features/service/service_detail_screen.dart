import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../models/subscription.dart';
import '../../providers/data_providers.dart';
import '../../widgets/status_chip.dart';

/// Full detail for one service: plan, connection (IP/login/MAC), validity
/// (start, expiry, days left) and billing mode. For prepaid services a Top-up
/// entry point is shown.
class ServiceDetailScreen extends ConsumerStatefulWidget {
  const ServiceDetailScreen({super.key, required this.service});

  final Subscription service;

  @override
  ConsumerState<ServiceDetailScreen> createState() =>
      _ServiceDetailScreenState();
}

class _ServiceDetailScreenState extends ConsumerState<ServiceDetailScreen> {
  bool _commandPending = false;

  Future<void> _reboot() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Restart device?'),
        content: const Text('Your connection will be interrupted briefly.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Restart'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _commandPending = true);
    try {
      final outcome = await ref
          .read(catalogRepositoryProvider)
          .rebootDevice(widget.service.id);
      if (mounted) _showOutcome(outcome.message, outcome.accepted);
    } catch (error) {
      if (mounted) _showOutcome('$error', false);
    } finally {
      if (mounted) setState(() => _commandPending = false);
    }
  }

  Future<void> _wifi() async {
    final ssid = TextEditingController();
    final password = TextEditingController();
    final submitted = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Update Wi-Fi'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: ssid,
              maxLength: 32,
              decoration: const InputDecoration(labelText: 'Wi-Fi name'),
            ),
            TextField(
              controller: password,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: 'New password (optional)',
              ),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Update'),
          ),
        ],
      ),
    );
    if (submitted != true || !mounted) return;
    setState(() => _commandPending = true);
    try {
      final outcome = await ref.read(catalogRepositoryProvider).updateWifi(
            widget.service.id,
            ssid: ssid.text.trim(),
            password:
                password.text.trim().isEmpty ? null : password.text.trim(),
          );
      if (mounted) _showOutcome(outcome.message, outcome.accepted);
    } catch (error) {
      if (mounted) _showOutcome('$error', false);
    } finally {
      ssid.dispose();
      password.dispose();
      if (mounted) setState(() => _commandPending = false);
    }
  }

  void _showOutcome(String message, bool success) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message),
        backgroundColor: success ? null : Theme.of(context).colorScheme.error,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.service;
    return Scaffold(
      appBar: AppBar(title: const Text('Service')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  s.displayName,
                  style: Theme.of(context).textTheme.titleLarge,
                ),
              ),
              StatusChip.fromPresentation(s.statusPresentation),
            ],
          ),
          if (s.planType != null) ...[
            const SizedBox(height: 4),
            Text(
              s.planType!,
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                    color: Theme.of(context).colorScheme.outline,
                  ),
            ),
          ],
          const SizedBox(height: 16),
          _ExpiryCard(service: s),
          const SizedBox(height: 12),
          _Section(
            title: 'Connection',
            rows: [
              _Row('IPv4 address', s.ipv4Address, copyable: true),
              if (s.ipv6Address != null)
                _Row('IPv6 address', s.ipv6Address, copyable: true),
              if (s.login != null) _Row('Login', s.login),
              if (s.macAddress != null) _Row('MAC address', s.macAddress),
            ],
          ),
          const SizedBox(height: 12),
          _Section(
            title: 'Plan',
            rows: [
              _Row('Billing', s.isPrepaid ? 'Prepaid' : 'Postpaid'),
              _Row('Started', Fmt.date(s.startAt)),
              // Postpaid doesn't expire on a date — show the next bill instead.
              if (s.hasExpiry)
                _Row('Expires', Fmt.date(s.expiresAt))
              else if (s.nextBillingAt != null)
                _Row('Next bill', Fmt.date(s.nextBillingAt)),
            ],
          ),
          const SizedBox(height: 24),
          OutlinedButton.icon(
            onPressed: _commandPending ? null : _reboot,
            icon: const Icon(Icons.restart_alt),
            label: const Text('Restart device'),
          ),
          const SizedBox(height: 12),
          OutlinedButton.icon(
            onPressed: _commandPending ? null : _wifi,
            icon: const Icon(Icons.wifi),
            label: const Text('Update Wi-Fi'),
          ),
          const SizedBox(height: 12),
          OutlinedButton.icon(
            onPressed: () =>
                context.push('/service/${s.id}/change-plan', extra: s),
            icon: const Icon(Icons.swap_horiz),
            label: const Text('Change plan'),
          ),
          const SizedBox(height: 12),
          OutlinedButton.icon(
            onPressed: () => context.push('/service/${s.id}/addons', extra: s),
            icon: const Icon(Icons.add_box_outlined),
            label: const Text('Add-ons'),
          ),
          const SizedBox(height: 12),
          OutlinedButton.icon(
            onPressed: () =>
                context.push('/service/${s.id}/buy-data', extra: s),
            icon: const Icon(Icons.data_usage),
            label: const Text('Buy data'),
          ),
          if (s.isPrepaid) ...[
            const SizedBox(height: 12),
            FilledButton.icon(
              onPressed: () => context.push('/topup'),
              icon: const Icon(Icons.add_card_outlined),
              label: const Text('Top up'),
            ),
          ],
        ],
      ),
    );
  }
}

class _ExpiryCard extends StatelessWidget {
  const _ExpiryCard({required this.service});
  final Subscription service;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final s = service;
    final days = s.daysUntilExpiry;
    final (color, label) = s.isExpired
        ? (scheme.error, 'Expired ${-days!} day${days == -1 ? '' : 's'} ago')
        : switch (days) {
            // Postpaid / no date expiry: show the next bill date, not a scary
            // "validity unknown".
            null => s.nextBillingAt != null
                ? (
                    context.semantic.success,
                    'Next bill ${Fmt.date(s.nextBillingAt)}',
                  )
                : (scheme.outline, 'No expiry date'),
            0 => (scheme.error, 'Expires today'),
            // Active service, stale billing date: running, not expired.
            < 0 => (context.semantic.success, 'Active'),
            <= 3 => (
                context.semantic.warning,
                '$days day${days == 1 ? '' : 's'} left',
              ),
            _ => (context.semantic.success, '$days days left'),
          };
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Icon(Icons.schedule, color: color),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    label,
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(
                          color: color,
                          fontWeight: FontWeight.w700,
                        ),
                  ),
                  if (s.expiresAt != null)
                    Text(
                      'Valid until ${Fmt.date(s.expiresAt)}',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section({required this.title, required this.rows});
  final String title;
  final List<Widget> rows;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.only(left: 4, bottom: 4),
          child: Text(title, style: Theme.of(context).textTheme.titleSmall),
        ),
        Card(
          child: Column(
            children: [
              for (var i = 0; i < rows.length; i++) ...[
                if (i > 0) const Divider(height: 1),
                rows[i],
              ],
            ],
          ),
        ),
      ],
    );
  }
}

class _Row extends StatelessWidget {
  const _Row(this.label, this.value, {this.copyable = false});
  final String label;
  final String? value;
  final bool copyable;

  @override
  Widget build(BuildContext context) {
    final v = value ?? '—';
    return ListTile(
      dense: true,
      title: Text(label),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Flexible(
            child: Text(
              v,
              textAlign: TextAlign.end,
              style: TextStyle(color: Theme.of(context).colorScheme.outline),
            ),
          ),
          if (copyable && value != null)
            IconButton(
              visualDensity: VisualDensity.compact,
              icon: const Icon(Icons.copy, size: 16),
              tooltip: 'Copy',
              onPressed: () {
                Clipboard.setData(ClipboardData(text: value!));
                ScaffoldMessenger.of(
                  context,
                ).showSnackBar(SnackBar(content: Text('$label copied')));
              },
            ),
        ],
      ),
    );
  }
}

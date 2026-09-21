import 'dart:async';

/// Raised when work targets an offline store whose session is ending.
///
/// This is deliberately different from Drift's "can't reopen" [StateError]:
/// callers are being fenced away from a retired store before its database is
/// closed, rather than discovering the closed connection afterwards.
class StoreDiscarded implements Exception {
  const StoreDiscarded(this.reason);

  final String reason;

  @override
  String toString() => 'StoreDiscarded: $reason';
}

/// Coordinates asynchronous work that belongs to one offline store.
///
/// The first caller to [stopAndDrain] closes admission. Work already admitted
/// is allowed to finish, including nested work in the same async zone. The
/// store can then close its database knowing that no admitted operation still
/// holds the old connection.
class StoreWorkGate {
  final Object _zoneKey = Object();
  bool _accepting = true;
  int _active = 0;
  Completer<void>? _drained;

  bool get isAccepting => _accepting;

  Future<T> run<T>(Future<T> Function() operation) {
    if (identical(Zone.current[_zoneKey], this)) {
      return Future<T>.sync(operation);
    }
    if (!_accepting) {
      return Future<T>.error(
        const StoreDiscarded('offline store session has ended'),
      );
    }

    _active++;
    final future = runZoned<Future<T>>(
      () => Future<T>.sync(operation),
      zoneValues: {_zoneKey: this},
    );
    return future.whenComplete(() {
      _active--;
      if (!_accepting && _active == 0) {
        final drained = _drained;
        if (drained != null && !drained.isCompleted) drained.complete();
      }
    });
  }

  void stopAccepting() {
    _accepting = false;
  }

  Future<void> waitUntilDrained() {
    if (_accepting) {
      throw StateError('work admission must stop before waiting for the drain');
    }
    if (_active == 0) return Future<void>.value();
    return (_drained ??= Completer<void>()).future;
  }

  Future<void> stopAndDrain() {
    stopAccepting();
    return waitUntilDrained();
  }
}

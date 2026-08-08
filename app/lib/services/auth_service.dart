import 'dart:async';

import 'package:flutter/foundation.dart';

import 'backend_service.dart';

/// Thin relay around the backend's login response. The backend is the
/// sole lockout authority (see python/backend/state.py) — it mirrors the
/// physical keypad's documented behavior: after 3 wrong attempts, lock
/// out for a cooldown and, if the system is armed, treat it as a
/// possible break-in. This class just reflects that back to the UI and
/// runs a local ticking countdown for the display; it doesn't decide
/// anything itself.
class AuthService extends ChangeNotifier {
  AuthService(this._backend);

  final BackendService _backend;

  bool _isAuthenticated = false;
  bool _isChecking = false;
  String? _error;
  bool _isLockedOut = false;
  int _lockoutRemainingSeconds = 0;
  Timer? _countdownTimer;

  bool get isAuthenticated => _isAuthenticated;
  bool get isChecking => _isChecking;
  String? get error => _error;
  bool get isLockedOut => _isLockedOut;
  Duration get lockoutRemaining => Duration(seconds: _lockoutRemainingSeconds);

  /// Submits [password] to the backend and updates local UI state from
  /// the result. Bails out immediately if already locked out — the
  /// backend would reject the attempt anyway, so this just saves a
  /// round trip and keeps the UI from flickering [isChecking] on/off for
  /// a request that can't possibly succeed. On a lockout response,
  /// starts the local countdown (see [_startCountdown]); on plain
  /// failure, surfaces the attempts-remaining count the backend reported
  /// so the user knows how close they are to being locked out.
  Future<void> login(String password) async {
    if (_isLockedOut) return;

    _isChecking = true;
    _error = null;
    notifyListeners();

    final result = await _backend.login(password);

    _isChecking = false;
    if (result.ok) {
      _isAuthenticated = true;
    } else if (result.lockedOut) {
      _isLockedOut = true;
      _lockoutRemainingSeconds = result.lockoutRemainingSeconds;
      _error = 'Too many failed attempts — locked out';
      _startCountdown();
    } else {
      _error = 'Wrong password (${result.attemptsRemaining} attempt(s) left)';
    }
    notifyListeners();
  }

  /// Ticks [_lockoutRemainingSeconds] down once per second purely for the
  /// UI countdown display — the backend is still the actual lockout
  /// authority (see class doc); this local timer doesn't re-check with
  /// it, it just counts down from the value the last login response
  /// reported, so it can drift slightly from the server's clock. Any
  /// existing timer is cancelled first as a defensive guard against
  /// starting two concurrent countdowns.
  void _startCountdown() {
    _countdownTimer?.cancel();
    _countdownTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      _lockoutRemainingSeconds--;
      if (_lockoutRemainingSeconds <= 0) {
        _isLockedOut = false;
        _error = null;
        _countdownTimer?.cancel();
      }
      notifyListeners();
    });
  }

  /// Signs the user out, revoking the session token on the backend as well
  /// as clearing the local flag.
  ///
  /// The server-side revoke is the part that matters: the backend now issues
  /// a real session token that stays valid for hours and can arm or disarm
  /// the system on its own, so a logout that only flipped a local boolean
  /// would leave a working credential behind. The local flag is cleared
  /// regardless of whether the revoke call reaches the backend — the user
  /// asked to sign out, and an unreachable server is not a reason to keep
  /// them signed in.
  Future<void> logout() async {
    try {
      await _backend.logout();
    } finally {
      _isAuthenticated = false;
      notifyListeners();
    }
  }

  /// Cancels the countdown timer so it doesn't keep firing (and calling
  /// notifyListeners on a disposed ChangeNotifier) after this service is
  /// torn down.
  @override
  void dispose() {
    _countdownTimer?.cancel();
    super.dispose();
  }
}

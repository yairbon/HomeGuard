import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../services/auth_service.dart';
import '../theme/app_theme.dart';
import '../widgets/homeguard_logo.dart';

/// The password gate shown by `_RootGate` (see `main.dart`) whenever
/// `AuthService.isAuthenticated` is false. Presents a single password field
/// and reflects [AuthService]'s in-progress/error/lockout state back to the
/// user.
class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

/// State for [LoginScreen]. Owns the password field's controller/focus
/// state and a local timer that keeps the lockout countdown text repainting
/// once per second.
class _LoginScreenState extends State<LoginScreen> {
  /// Backs the password [TextFormField].
  final _passwordController = TextEditingController();

  /// Key for the [Form] wrapping the password field, used to run
  /// [FormState.validate] before submitting.
  final _formKey = GlobalKey<FormState>();

  /// Whether the password field is currently masked (obscured). Toggled by
  /// the field's visibility-eye suffix icon.
  bool _obscure = true;

  /// Local 1-second repaint timer, active only while [AuthService.isLockedOut]
  /// is true. See [_ensureTicker] for why this exists.
  Timer? _ticker;

  /// Releases the text controller and stops the countdown ticker so it
  /// doesn't keep firing (and calling setState) after this widget is gone.
  @override
  void dispose() {
    _passwordController.dispose();
    _ticker?.cancel();
    super.dispose();
  }

  /// Runs form validation and, if the password field isn't empty, forwards
  /// the entered password to [AuthService.login]. Does nothing on failed
  /// validation — the [TextFormField.validator] below is what surfaces the
  /// "Enter the password" error in that case.
  void _submit(AuthService auth) {
    if (_formKey.currentState?.validate() ?? false) {
      auth.login(_passwordController.text);
    }
  }

  /// Makes sure a 1-second repaint [Timer] is running while the account is
  /// locked out, so the "try again in Ns" countdown in [build] visibly
  /// ticks down instead of only updating whenever some other state change
  /// happens to trigger a rebuild.
  ///
  /// Called on every [build], so the `_ticker == null` guard is what stops
  /// it from spawning a new competing [Timer.periodic] on every single
  /// rebuild while still locked out — without it we'd leak a fresh timer
  /// each time [AuthService] notifies listeners.
  ///
  /// The timer cancels and clears itself as soon as `auth.isLockedOut` goes
  /// false, so it doesn't keep ticking (and calling setState) once the
  /// lockout ends.
  ///
  /// Note: [AuthService] already runs its own per-second countdown
  /// internally (`AuthService._startCountdown`) that calls
  /// `notifyListeners()`, which on its own would already rebuild this
  /// screen via `context.watch<AuthService>()` below. This local ticker's
  /// `setState` may therefore be redundant with that — flagging it here
  /// rather than changing the behavior.
  void _ensureTicker(AuthService auth) {
    if (auth.isLockedOut && _ticker == null) {
      _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
        if (mounted) setState(() {});
        if (!auth.isLockedOut) {
          _ticker?.cancel();
          _ticker = null;
        }
      });
    }
  }

  /// Builds the login form: an animated shield badge, a title, a
  /// status/error line, the password field, and the submit button — all of
  /// which react to the watched [AuthService]'s checking/error/lockout state.
  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthService>();
    _ensureTicker(auth);
    final locked = auth.isLockedOut;

    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: Form(
              key: _formKey,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  // One-shot entrance animation for the shield badge: runs
                  // automatically from 0 to 1 as soon as this widget is
                  // built (TweenAnimationBuilder has no controller to
                  // start/stop — it just animates whenever its `tween`
                  // changes, which here happens once on first build), fading
                  // and sliding the badge up into place over 500ms.
                  TweenAnimationBuilder<double>(
                    tween: Tween(begin: 0, end: 1),
                    duration: const Duration(milliseconds: 500),
                    curve: Curves.easeOut,
                    builder: (context, value, child) => Opacity(
                      opacity: value,
                      child: Transform.translate(
                        offset: Offset(0, (1 - value) * 12),
                        child: child,
                      ),
                    ),
                    child: Container(
                      width: 88,
                      height: 88,
                      alignment: Alignment.center,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: AppColors.system.withValues(alpha: 0.12),
                      ),
                      child: const HomeGuardLogo(size: 56),
                    ),
                  ),
                  const SizedBox(height: 20),
                  Text(
                    'HOMEGUARD',
                    style: AppTheme.displayStyle(
                      fontSize: 38,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 4,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    locked
                        ? 'Locked out — try again in ${auth.lockoutRemaining.inSeconds}s'
                        : 'Enter the system password to continue',
                    style: TextStyle(color: locked ? AppColors.alarm : AppColors.textMuted),
                  ),
                  const SizedBox(height: 40),
                  TextFormField(
                    controller: _passwordController,
                    obscureText: _obscure,
                    autofocus: true,
                    // Disabled while locked out so the user can't keep
                    // typing/submitting during the cooldown; the lockout
                    // message above already explains why.
                    enabled: !locked,
                    style: const TextStyle(color: AppColors.textPrimary),
                    decoration: InputDecoration(
                      labelText: 'Password',
                      // Suppressed while locked out because the lockout
                      // banner text above already conveys the relevant
                      // error state; showing AuthService's last wrong-
                      // password error at the same time would be redundant.
                      errorText: locked ? null : auth.error,
                      suffixIcon: IconButton(
                        icon: Icon(
                          _obscure ? Icons.visibility_off : Icons.visibility,
                          color: AppColors.textMuted,
                        ),
                        onPressed: () => setState(() => _obscure = !_obscure),
                      ),
                    ),
                    validator: (value) =>
                        (value == null || value.isEmpty) ? 'Enter the password' : null,
                    // Enter/submit from the keyboard is a no-op while locked
                    // out, mirroring the disabled submit button below.
                    onFieldSubmitted: (_) => locked ? null : _submit(auth),
                  ),
                  const SizedBox(height: 24),
                  // Submit button: disabled while a login request is in
                  // flight (auth.isChecking, to prevent double-submits) or
                  // while locked out. Swaps its label for a small spinner
                  // during the in-flight state, and reads "LOCKED" instead
                  // of "UNLOCK" during lockout.
                  SizedBox(
                    width: double.infinity,
                    child: ElevatedButton(
                      onPressed: (auth.isChecking || locked) ? null : () => _submit(auth),
                      child: auth.isChecking
                          ? const SizedBox(
                              width: 20,
                              height: 20,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : Text(locked ? 'LOCKED' : 'UNLOCK'),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

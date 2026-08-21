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

/// State for [LoginScreen]. Owns the password field's controller and
/// obscure/visibility state; the lockout countdown is watched straight off
/// [AuthService] (see [build]).
class _LoginScreenState extends State<LoginScreen> {
  /// Backs the password [TextFormField].
  final _passwordController = TextEditingController();

  /// Key for the [Form] wrapping the password field, used to run
  /// [FormState.validate] before submitting.
  final _formKey = GlobalKey<FormState>();

  /// Whether the password field is currently masked (obscured). Toggled by
  /// the field's visibility-eye suffix icon.
  bool _obscure = true;

  /// Releases the text controller.
  @override
  void dispose() {
    _passwordController.dispose();
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

  /// Builds the login form: an animated shield badge, a title, a
  /// status/error line, the password field, and the submit button — all of
  /// which react to the watched [AuthService]'s checking/error/lockout state.
  ///
  /// This screen used to run its own `Timer.periodic` calling `setState`
  /// every second so the lockout countdown would visibly tick. That was a
  /// second timer *and* a second full-screen rebuild on top of the one
  /// `AuthService._startCountdown` already caused via `notifyListeners()` —
  /// the old code's own comment said as much. Now the service ticks a
  /// [ValueNotifier] and only notifies on real transitions, and the one
  /// [Text] that shows the number subscribes to it directly.
  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthService>();
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
                    style: AppTheme.displayBrand,
                  ),
                  const SizedBox(height: 8),
                  if (locked)
                    ValueListenableBuilder<int>(
                      valueListenable: auth.lockoutSecondsRemaining,
                      builder: (context, seconds, _) => Text(
                        'Locked out — try again in ${seconds}s',
                        style: const TextStyle(color: AppColors.alarm),
                      ),
                    )
                  else
                    Text(
                      'Enter the system password to continue',
                      style: TextStyle(color: AppColors.textMuted),
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

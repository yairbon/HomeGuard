import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../services/auth_service.dart';
import '../services/backend_config.dart';
import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import '../widgets/homeguard_icons.dart';

/// Account and system settings screen.
///
/// Covers four independent areas: a night-mode toggle, emergency-contact
/// details plus the police-call response delay, a change-password form,
/// and the backend URL. The night-mode switch and emergency-contact
/// fields persist to [BackendService] as soon as they change; the password
/// form is only submitted when the user taps its button (see
/// [_submitPassword]). The backend URL persists to local device storage
/// via [BackendConfig] instead — see [_saveBackendUrl].
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  final _formKey = GlobalKey<FormState>();
  final _oldController = TextEditingController();
  final _newController = TextEditingController();
  final _confirmController = TextEditingController();
  final _emergencyController = TextEditingController();
  final _backendUrlController = TextEditingController();

  bool _isSubmitting = false;
  String? _error;
  String? _success;

  bool _nightMode = false;
  double _policeCallDelay = 60;
  // Stays false until _loadSettings() has fetched the real values from the
  // backend, so the body shows a spinner instead of briefly rendering
  // controls pre-populated with the hardcoded defaults above.
  bool _settingsLoaded = false;

  // True right after the backend-URL save button is tapped, until the next
  // build; shows a one-off "restart to apply" hint rather than a permanent
  // one, since most visits to this screen aren't about that field.
  bool _backendUrlSaved = false;

  @override
  void initState() {
    super.initState();
    _loadSettings();
    // Independent of _loadSettings: this comes from local device storage
    // (BackendConfig/SharedPreferences), not the backend, so it doesn't
    // need to wait on — or block — the network round-trip the other
    // settings make.
    BackendConfig.load().then((url) {
      if (!mounted) return;
      setState(() => _backendUrlController.text = url);
    });
  }

  /// Persists the backend-URL field to [BackendConfig] and flips on the
  /// one-off "restart to apply" hint (see [_backendUrlSaved]).
  ///
  /// [BackendConfig.save] normalizes what was typed (adds a missing
  /// scheme, drops a trailing slash — see [BackendConfig.normalize]) and
  /// returns what it actually stored; that value is written back into the
  /// field so the user sees the address the app will really use rather
  /// than their raw input. Beyond that normalization there's no
  /// reachability check: an unreachable-but-well-formed value just means
  /// [HttpBackendService] fails to connect after restart, surfaced the
  /// same way any other backend-offline state is.
  Future<void> _saveBackendUrl() async {
    final saved = await BackendConfig.save(_backendUrlController.text);
    if (!mounted) return;
    setState(() {
      _backendUrlController.text = saved;
      _backendUrlSaved = true;
    });
  }

  /// Fetches night mode, emergency number, and police-call delay from
  /// [BackendService] and populates the corresponding state.
  ///
  /// The three requests are issued together via [Future.wait] rather than
  /// awaited one at a time, so the loading spinner is shown for only as
  /// long as the slowest request takes, not the sum of all three. The
  /// `mounted` check guards the subsequent [setState] in case the user has
  /// already navigated away from this screen by the time the requests
  /// resolve.
  Future<void> _loadSettings() async {
    final backend = context.read<BackendService>();
    final results = await Future.wait([
      backend.getNightMode(),
      backend.getEmergencyNumber(),
      backend.getPoliceCallDelaySeconds(),
    ]);
    if (!mounted) return;
    setState(() {
      _nightMode = results[0] as bool;
      _emergencyController.text = results[1] as String;
      _policeCallDelay = (results[2] as int).toDouble();
      _settingsLoaded = true;
    });
  }

  /// Disposes the four text controllers backing the password-change form
  /// (and the emergency-number field) to release their resources.
  @override
  void dispose() {
    _oldController.dispose();
    _newController.dispose();
    _confirmController.dispose();
    _emergencyController.dispose();
    _backendUrlController.dispose();
    super.dispose();
  }

  /// Validates and submits the change-password form.
  ///
  /// Returns early without doing anything if form validation fails (e.g.
  /// the confirmation field doesn't match). While the request is in
  /// flight, [_isSubmitting] disables the submit button and swaps its
  /// label for a spinner so the user can't double-submit. On success the
  /// three password fields are cleared and a success message is shown; on
  /// failure [_error] is set instead so the reason is visible to the user.
  Future<void> _submitPassword() async {
    if (!(_formKey.currentState?.validate() ?? false)) return;

    setState(() {
      _isSubmitting = true;
      _error = null;
      _success = null;
    });

    final backend = context.read<BackendService>();
    final ok = await backend.changePassword(_oldController.text, _newController.text);

    // The request can outlive this screen — the user can tab away or log
    // out while it's in flight — and setState on a disposed State throws.
    // Every other awaiting method here already guards this way.
    if (!mounted) return;

    setState(() {
      _isSubmitting = false;
      if (ok) {
        _success = 'Password changed';
        _oldController.clear();
        _newController.clear();
        _confirmController.clear();
      } else {
        _error = 'Current password is incorrect';
      }
    });
  }

  /// Builds the settings screen.
  ///
  /// Shows a centered spinner until [_settingsLoaded] is true (see
  /// [_loadSettings]), then renders the night-mode switch, emergency
  /// contact section, change-password form, and backend-URL section, each
  /// inside its own [_SectionCard]. The backend-URL section doesn't wait
  /// on [_settingsLoaded] conceptually (it loads independently in
  /// [initState]) but is still gated behind the same spinner for a
  /// simpler single-loading-state screen rather than sections popping in
  /// at different times.
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('SETTINGS'),
        actions: [
          IconButton(
            icon: const HomeGuardIcon(HGIcon.logout, size: 20),
            tooltip: 'Log out',
            onPressed: () => context.read<AuthService>().logout(),
          ),
        ],
      ),
      body: !_settingsLoaded
          ? const Center(child: CircularProgressIndicator())
          : SingleChildScrollView(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _SectionCard(
                    child: SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      value: _nightMode,
                      activeThumbColor: AppColors.system,
                      onChanged: (value) async {
                        // Flip the switch immediately (optimistic update) so the
                        // UI feels responsive, then persist to the backend; the
                        // switch doesn't wait for the write to complete or roll
                        // back on failure.
                        setState(() => _nightMode = value);
                        await context.read<BackendService>().setNightMode(value);
                      },
                      title: const Text('Night mode', style: TextStyle(color: AppColors.textPrimary)),
                      subtitle: Text(
                        'Automatically arm the system overnight',
                        style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                      ),
                      secondary: const HomeGuardIcon(HGIcon.nightMode, color: AppColors.system),
                    ),
                  ),
                  const SizedBox(height: 20),
                  _SectionCard(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            const HomeGuardIcon(HGIcon.emergency, color: AppColors.system, size: 20),
                            const SizedBox(width: 10),
                            const Text('Emergency contact', style: TextStyle(color: AppColors.textPrimary)),
                          ],
                        ),
                        const SizedBox(height: 6),
                        Text(
                          "Auto-called if an alarm isn't cleared in time",
                          style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                        ),
                        const SizedBox(height: 14),
                        Row(
                          children: [
                            Expanded(
                              child: TextField(
                                controller: _emergencyController,
                                keyboardType: TextInputType.phone,
                                style: const TextStyle(color: AppColors.textPrimary),
                                decoration: const InputDecoration(labelText: 'Phone number'),
                              ),
                            ),
                            const SizedBox(width: 10),
                            IconButton.filled(
                              onPressed: () => context
                                  .read<BackendService>()
                                  .setEmergencyNumber(_emergencyController.text.trim()),
                              icon: const Icon(Icons.save_outlined),
                            ),
                          ],
                        ),
                        const SizedBox(height: 18),
                        Text(
                          'Response window before calling: ${_policeCallDelay.round()}s',
                          style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                        ),
                        Slider(
                          value: _policeCallDelay,
                          min: 15,
                          max: 180,
                          divisions: 33,
                          activeColor: AppColors.system,
                          label: '${_policeCallDelay.round()}s',
                          // Update local state on every drag frame for a smooth
                          // slider, but only write to the backend once the user
                          // releases it (onChangeEnd) — persisting on every
                          // onChanged tick would fire a request per pixel of drag.
                          onChanged: (value) => setState(() => _policeCallDelay = value),
                          onChangeEnd: (value) => context
                              .read<BackendService>()
                              .setPoliceCallDelaySeconds(value.round()),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 20),
                  _SectionCard(
                    child: Form(
                      key: _formKey,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            children: [
                              const HomeGuardIcon(HGIcon.auth, color: AppColors.system, size: 20),
                              const SizedBox(width: 10),
                              const Text('Change password', style: TextStyle(color: AppColors.textPrimary)),
                            ],
                          ),
                          const SizedBox(height: 16),
                          TextFormField(
                            controller: _oldController,
                            obscureText: true,
                            decoration: const InputDecoration(labelText: 'Current password'),
                            validator: (v) => (v == null || v.isEmpty) ? 'Required' : null,
                          ),
                          const SizedBox(height: 12),
                          TextFormField(
                            controller: _newController,
                            obscureText: true,
                            decoration: const InputDecoration(labelText: 'New password'),
                            validator: (v) =>
                                (v == null || v.length < 4) ? 'At least 4 characters' : null,
                          ),
                          const SizedBox(height: 12),
                          TextFormField(
                            controller: _confirmController,
                            obscureText: true,
                            decoration: const InputDecoration(labelText: 'Confirm new password'),
                            validator: (v) =>
                                v != _newController.text ? 'Passwords do not match' : null,
                          ),
                          if (_error != null) ...[
                            const SizedBox(height: 12),
                            Text(_error!, style: const TextStyle(color: AppColors.alarm)),
                          ],
                          if (_success != null) ...[
                            const SizedBox(height: 12),
                            Text(_success!, style: const TextStyle(color: AppColors.safe)),
                          ],
                          const SizedBox(height: 20),
                          SizedBox(
                            width: double.infinity,
                            child: ElevatedButton(
                              onPressed: _isSubmitting ? null : _submitPassword,
                              child: _isSubmitting
                                  ? const SizedBox(
                                      width: 20,
                                      height: 20,
                                      child: CircularProgressIndicator(strokeWidth: 2),
                                    )
                                  : const Text('UPDATE PASSWORD'),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 20),
                  _SectionCard(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            const HomeGuardIcon(HGIcon.settings, color: AppColors.system, size: 20),
                            const SizedBox(width: 10),
                            const Text('Backend connection',
                                style: TextStyle(color: AppColors.textPrimary)),
                          ],
                        ),
                        const SizedBox(height: 6),
                        Text(
                          'Where the app looks for the backend. Use a private-network '
                          '(e.g. Tailscale) address to reach it off your home WiFi.',
                          style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                        ),
                        const SizedBox(height: 14),
                        Row(
                          children: [
                            Expanded(
                              child: TextField(
                                controller: _backendUrlController,
                                keyboardType: TextInputType.url,
                                style: const TextStyle(color: AppColors.textPrimary),
                                decoration: const InputDecoration(
                                  labelText: 'Backend URL',
                                  hintText: 'http://100.x.x.x:8000',
                                ),
                                onChanged: (_) {
                                  if (_backendUrlSaved) setState(() => _backendUrlSaved = false);
                                },
                              ),
                            ),
                            const SizedBox(width: 10),
                            IconButton.filled(
                              onPressed: _saveBackendUrl,
                              icon: const Icon(Icons.save_outlined),
                            ),
                          ],
                        ),
                        if (_backendUrlSaved) ...[
                          const SizedBox(height: 10),
                          Text(
                            'Saved — restart the app for this to take effect.',
                            style: TextStyle(color: AppColors.safe, fontSize: 12),
                          ),
                        ],
                      ],
                    ),
                  ),
                ],
              ),
            ),
    );
  }
}

/// Rounded, padded surface used to visually group each settings section
/// (night mode, emergency contact, change password) on the screen.
class _SectionCard extends StatelessWidget {
  const _SectionCard({required this.child});

  final Widget child;

  /// Wraps [child] in the card's padding and rounded background.
  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.surfaceRaised,
        borderRadius: BorderRadius.circular(12),
      ),
      child: child,
    );
  }
}

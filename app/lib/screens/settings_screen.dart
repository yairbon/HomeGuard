import 'package:flutter/material.dart';
// For FilteringTextInputFormatter — the keypad PIN fields accept digits only.
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../services/auth_service.dart';
import '../services/backend_config.dart';
import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import '../widgets/homeguard_icons.dart';

/// Account and system settings screen.
///
/// Covers five independent areas: a night-mode toggle, emergency-contact
/// details plus the police-call response delay, a change-password form, the
/// physical keypad's PIN, and the backend URL. The night-mode switch and
/// emergency-contact fields persist to [BackendService] as soon as they
/// change; the password and PIN forms are only submitted when the user taps
/// their button (see [_submitPassword], [_submitKeypadPin]). The backend URL
/// persists to local device storage via [BackendConfig] instead — see
/// [_saveBackendUrl].
///
/// The two credential forms are unrelated despite sitting next to each
/// other: [_submitPassword] changes how you sign in to *this app*, while
/// [_submitKeypadPin] changes the code typed on the *hardware* keypad by the
/// door. Only the second one leaves the backend.
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
  final _ntfyServerController = TextEditingController();
  final _ntfyTopicController = TextEditingController();
  final _pinFormKey = GlobalKey<FormState>();
  final _pinController = TextEditingController();
  final _pinConfirmController = TextEditingController();

  bool _isSubmittingPin = false;
  String? _pinError;
  String? _pinSuccess;
  // Set when the PIN was saved on the backend but the door controller didn't
  // answer. Kept apart from _pinSuccess because it is neither a success nor a
  // failure: the change is recorded, and the keypad is still on the old PIN.
  String? _pinWarning;

  bool _isSavingNtfy = false;
  String? _ntfyMessage;

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
  /// one-off "restart to apply" hint (see [_backendUrlSaved]). Doesn't
  /// validate the URL's shape — an unreachable/malformed value just means
  /// [HttpBackendService] fails to connect after restart, surfaced the
  /// same way any other backend-offline state is (see [_SectionCard]'s
  /// sibling screens' connectivity indicators), which is simpler than
  /// duplicating URL-validity rules the app doesn't otherwise need.
  Future<void> _saveBackendUrl() async {
    await BackendConfig.save(_backendUrlController.text);
    if (!mounted) return;
    setState(() => _backendUrlSaved = true);
  }

  /// Persists the ntfy server/topic to the backend.
  ///
  /// Unlike the night-mode switch, this isn't optimistic: the value only
  /// matters if the backend actually stored it, and silently appearing to
  /// have enabled alerts that were never saved would be worse than a
  /// moment's latency.
  Future<void> _saveNtfy() async {
    setState(() {
      _isSavingNtfy = true;
      _ntfyMessage = null;
    });
    await context.read<BackendService>().setNtfyConfig(
          _ntfyServerController.text.trim(),
          _ntfyTopicController.text.trim(),
        );
    if (!mounted) return;
    setState(() {
      _isSavingNtfy = false;
      _ntfyMessage = _ntfyTopicController.text.trim().isEmpty
          ? 'Push notifications disabled'
          : 'Saved';
    });
  }

  /// Saves first, then asks the backend to push a test notification —
  /// saving first so the button tests what's on screen rather than whatever
  /// was stored on a previous visit.
  Future<void> _testNtfy() async {
    await _saveNtfy();
    if (!mounted) return;
    setState(() => _isSavingNtfy = true);
    final sent = await context.read<BackendService>().sendTestNotification();
    if (!mounted) return;
    setState(() {
      _isSavingNtfy = false;
      _ntfyMessage = sent
          ? 'Test sent — check the ntfy app'
          : 'Set a topic first';
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
      backend.getNtfyConfig(),
    ]);
    if (!mounted) return;
    final ntfy = results[3] as NtfyConfig;
    setState(() {
      _nightMode = results[0] as bool;
      _emergencyController.text = results[1] as String;
      _policeCallDelay = (results[2] as int).toDouble();
      _ntfyServerController.text = ntfy.server;
      _ntfyTopicController.text = ntfy.topic;
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
    _ntfyServerController.dispose();
    _ntfyTopicController.dispose();
    _pinController.dispose();
    _pinConfirmController.dispose();
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

    // Guard, like every other awaited handler on this screen: the user can
    // navigate away mid-request, and setState on a disposed State throws.
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

  /// Validates and submits the keypad-PIN form.
  ///
  /// Reports three outcomes rather than two, because "saved, but the board
  /// didn't answer" is a genuinely different situation from either success
  /// or failure: the change is recorded and will be re-sent, but until the
  /// door controller takes it, **the keypad still opens on the old PIN**.
  /// Telling the user that plainly is the whole point — an alarm panel that
  /// says "PIN changed" when the hardware disagrees is worse than one that
  /// says nothing.
  Future<void> _submitKeypadPin() async {
    if (!(_pinFormKey.currentState?.validate() ?? false)) return;

    setState(() {
      _isSubmittingPin = true;
      _pinError = null;
      _pinSuccess = null;
      _pinWarning = null;
    });

    final backend = context.read<BackendService>();
    final result = await backend.setKeypadPin(_pinController.text);

    if (!mounted) return;
    setState(() {
      _isSubmittingPin = false;
      if (!result.reachable) {
        _pinError = "Couldn't reach the backend — check the URL below";
      } else if (!result.ok) {
        _pinError = 'PIN must be 4-8 digits';
      } else {
        _pinController.clear();
        _pinConfirmController.clear();
        if (result.hardware) {
          _pinSuccess = result.secretsUpdated
              ? 'Keypad PIN updated, and secrets.h synced for the next flash'
              : 'Keypad PIN updated on the keypad';
        } else {
          _pinWarning = 'Saved, but the door controller did not answer — '
              'the keypad still uses the old PIN. It will be sent again '
              'when the board is back online.';
        }
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
                            const Text(
                              'Push notifications',
                              style: TextStyle(color: AppColors.textPrimary),
                            ),
                          ],
                        ),
                        const SizedBox(height: 6),
                        Text(
                          'Alerts reach your phone even when HomeGuard is closed. '
                          'Install the ntfy app and subscribe to the same topic.',
                          style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                        ),
                        const SizedBox(height: 14),
                        TextField(
                          controller: _ntfyServerController,
                          keyboardType: TextInputType.url,
                          autocorrect: false,
                          style: const TextStyle(color: AppColors.textPrimary),
                          decoration: const InputDecoration(labelText: 'ntfy server'),
                        ),
                        const SizedBox(height: 10),
                        TextField(
                          controller: _ntfyTopicController,
                          autocorrect: false,
                          style: const TextStyle(color: AppColors.textPrimary),
                          decoration: const InputDecoration(
                            labelText: 'Topic (leave empty to disable)',
                          ),
                        ),
                        const SizedBox(height: 8),
                        // Not a footnote: on the public ntfy.sh the topic name
                        // is the only thing stopping a stranger from reading
                        // this house's alarm notifications.
                        Text(
                          'Anyone who knows the topic name can read your alerts. '
                          'Use a long random name, or self-host.',
                          style: TextStyle(color: AppColors.transitional, fontSize: 11),
                        ),
                        const SizedBox(height: 12),
                        Row(
                          children: [
                            Expanded(
                              child: OutlinedButton(
                                onPressed: _isSavingNtfy ? null : _saveNtfy,
                                child: const Text('SAVE'),
                              ),
                            ),
                            const SizedBox(width: 10),
                            Expanded(
                              child: OutlinedButton(
                                // Sending a test before relying on this is the
                                // difference between "configured" and "works".
                                onPressed: _isSavingNtfy ? null : _testNtfy,
                                child: const Text('SEND TEST'),
                              ),
                            ),
                          ],
                        ),
                        if (_ntfyMessage != null) ...[
                          const SizedBox(height: 10),
                          Text(
                            _ntfyMessage!,
                            style: TextStyle(color: AppColors.safe, fontSize: 12),
                          ),
                        ],
                      ],
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
                        // Reworded to match what actually happens. This used
                        // to say the number would be "auto-called", which no
                        // code anywhere did — placing a real call needs a
                        // telephony provider. What the system does do is push
                        // a second, urgent notification naming this contact
                        // if nobody clears the alarm in time.
                        Text(
                          "If an alarm isn't cleared in time, an urgent notification "
                          'tells you to call this number.',
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
                          'Escalate if not cleared within: ${_policeCallDelay.round()}s',
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
                    child: Form(
                      key: _pinFormKey,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            children: [
                              const HomeGuardIcon(HGIcon.auth, color: AppColors.system, size: 20),
                              const SizedBox(width: 10),
                              const Text('Keypad PIN', style: TextStyle(color: AppColors.textPrimary)),
                            ],
                          ),
                          const SizedBox(height: 6),
                          const Text(
                            'The code typed on the physical keypad to disarm. '
                            'Sent to the door controller, which stores it and '
                            'keeps it across reboots.',
                            style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                          ),
                          const SizedBox(height: 16),
                          TextFormField(
                            controller: _pinController,
                            obscureText: true,
                            keyboardType: TextInputType.number,
                            // Digits only: the keypad's other two keys are '*'
                            // (arm) and '#' (submit), so a PIN containing
                            // anything else could never be typed on it.
                            inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                            maxLength: 8,
                            decoration: const InputDecoration(
                              labelText: 'New keypad PIN',
                              counterText: '',
                            ),
                            validator: (v) => (v == null || v.length < 4)
                                ? 'At least 4 digits'
                                : null,
                          ),
                          const SizedBox(height: 12),
                          TextFormField(
                            controller: _pinConfirmController,
                            obscureText: true,
                            keyboardType: TextInputType.number,
                            inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                            maxLength: 8,
                            decoration: const InputDecoration(
                              labelText: 'Confirm keypad PIN',
                              counterText: '',
                            ),
                            validator: (v) =>
                                v != _pinController.text ? 'PINs do not match' : null,
                          ),
                          if (_pinError != null) ...[
                            const SizedBox(height: 12),
                            Text(_pinError!, style: const TextStyle(color: AppColors.alarm)),
                          ],
                          if (_pinWarning != null) ...[
                            const SizedBox(height: 12),
                            Text(
                              _pinWarning!,
                              style: const TextStyle(color: AppColors.transitional),
                            ),
                          ],
                          if (_pinSuccess != null) ...[
                            const SizedBox(height: 12),
                            Text(_pinSuccess!, style: const TextStyle(color: AppColors.safe)),
                          ],
                          const SizedBox(height: 20),
                          SizedBox(
                            width: double.infinity,
                            child: ElevatedButton(
                              onPressed: _isSubmittingPin ? null : _submitKeypadPin,
                              child: _isSubmittingPin
                                  ? const SizedBox(
                                      width: 20,
                                      height: 20,
                                      child: CircularProgressIndicator(strokeWidth: 2),
                                    )
                                  : const Text('UPDATE KEYPAD PIN'),
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
  ///
  /// A [Material] rather than a `Container` with a `BoxDecoration` colour,
  /// even though the two look identical. Interactive children — the
  /// [SwitchListTile] in the night-mode section — paint their ink splash on
  /// the nearest [Material] ancestor, which used to be somewhere further up
  /// the tree, *behind* this card's opaque background. The tap feedback was
  /// therefore drawn and then immediately hidden, and Flutter asserts on
  /// exactly this arrangement.
  @override
  Widget build(BuildContext context) {
    return Material(
      color: AppColors.surfaceRaised,
      borderRadius: BorderRadius.circular(12),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: SizedBox(width: double.infinity, child: child),
      ),
    );
  }
}

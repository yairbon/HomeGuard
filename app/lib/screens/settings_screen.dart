import 'package:flutter/material.dart';
// For FilteringTextInputFormatter — the new-password field accepts digits
// only, since it doubles as the physical keypad's PIN.
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../services/auth_service.dart';
import '../services/backend_config.dart';
import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import '../widgets/homeguard_icons.dart';

/// Account and system settings screen.
///
/// Covers five independent areas: a night-mode toggle, push notifications,
/// emergency-contact details plus the police-call response delay, a
/// change-password form, and the backend URL. The night-mode switch and
/// emergency-contact fields persist to [BackendService] as soon as they
/// change; the password form is only submitted when the user taps its
/// button (see [_submitPassword]). The backend URL persists to local device
/// storage via [BackendConfig] instead — see [_saveBackendUrl].
///
/// The password form used to sit next to an unrelated second one: this
/// app's login and the physical keypad's disarm PIN were independent
/// credentials. The backend merged them into a single shared secret at the
/// project owner's explicit request (see python/README.md's "Login
/// password / keypad PIN: one shared secret" for the full rationale), so
/// [_submitPassword] now changes both at once — how you sign in to *this
/// app*, and the code typed on the *hardware* keypad by the door. The new
/// value must be 4-8 digits, the one shape a physical keypad can type, even
/// though it is now also the app's login credential.
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

  bool _isSavingNtfy = false;
  String? _ntfyMessage;

  bool _isSubmitting = false;
  String? _error;
  String? _success;
  // Set when the new value was saved on the backend (this app can already
  // log in with it) but the door controller didn't answer. Kept apart from
  // _success because it is neither a success nor a failure: the change is
  // recorded, and the physical keypad is still on the old code.
  String? _warning;

  bool _nightMode = false;
  int _nightModeArmHour = 23;
  int _nightModeDisarmHour = 7;

  /// The escalation delay, as a notifier rather than plain state.
  ///
  /// A [Slider]'s `onChanged` fires once per pointer sample. Calling
  /// `setState` from there rebuilt all five section cards and seven text
  /// fields, dozens of times per drag, to move one integer inside a single
  /// [Text]. Only that [Text] and the slider itself listen now.
  final _policeCallDelay = ValueNotifier<double>(60);

  // Stays false until _loadSettings() has fetched the real values from the
  // backend, so the body shows a spinner instead of briefly rendering
  // controls pre-populated with the hardcoded defaults above.
  bool _settingsLoaded = false;

  // Set when the load failed outright (backend unreachable or session
  // rejected). Distinct from "still loading": the screen offers a retry
  // instead of spinning forever.
  bool _settingsFailed = false;

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

  /// Fetches every backend-stored setting and populates the form.
  ///
  /// One request, not four: this used to call four separate getters, each of
  /// which fetched the whole of `/state` (event log included) to pull out one
  /// field. More importantly, each of those getters swallowed transport
  /// failures and fell back to its own default — so an unreachable backend
  /// produced a screen that looked fully loaded, showing a blank emergency
  /// number and a blank ntfy topic as if that were the real configuration.
  /// One tap on save then wrote the blanks back, turning off the only alert
  /// channel that works when the app is closed. A null result now means
  /// exactly what it says, and the screen refuses to pretend otherwise.
  ///
  /// The `mounted` check guards the subsequent [setState] in case the user
  /// has already navigated away by the time the request resolves.
  Future<void> _loadSettings() async {
    final settings = await context.read<BackendService>().getSettings();
    if (!mounted) return;
    setState(() {
      if (settings == null) {
        _settingsFailed = true;
        return;
      }
      _nightMode = settings.nightMode;
      _nightModeArmHour = settings.nightModeArmHour;
      _nightModeDisarmHour = settings.nightModeDisarmHour;
      _emergencyController.text = settings.emergencyNumber;
      _policeCallDelay.value = settings.policeCallDelaySeconds.toDouble();
      _ntfyServerController.text = settings.ntfy.server;
      _ntfyTopicController.text = settings.ntfy.topic;
      _settingsFailed = false;
      _settingsLoaded = true;
    });
  }

  /// Re-runs [_loadSettings] from the retry button, putting the screen back
  /// into its loading state first.
  void _retryLoadSettings() {
    setState(() => _settingsFailed = false);
    _loadSettings();
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
    _policeCallDelay.dispose();
    super.dispose();
  }

  /// Validates and submits the merged password/keypad-PIN form.
  ///
  /// Returns early without doing anything if form validation fails (e.g.
  /// the confirmation field doesn't match). While the request is in
  /// flight, [_isSubmitting] disables the submit button and swaps its
  /// label for a spinner so the user can't double-submit.
  ///
  /// Reports three outcomes rather than two, because "saved, but the board
  /// didn't answer" is a genuinely different situation from either success
  /// or failure: the change is recorded and will be re-sent, but until the
  /// door controller takes it, **the keypad still opens on the old code**.
  /// Telling the user that plainly is the whole point — a panel that says
  /// "updated" when the hardware disagrees is worse than one that says
  /// nothing. (This used to be [_submitKeypadPin], a separate form for a
  /// separate credential — see the class doc for why the two merged.)
  Future<void> _submitPassword() async {
    if (!(_formKey.currentState?.validate() ?? false)) return;

    setState(() {
      _isSubmitting = true;
      _error = null;
      _success = null;
      _warning = null;
    });

    final backend = context.read<BackendService>();
    final result = await backend.changePassword(_oldController.text, _newController.text);

    // Guard, like every other awaited handler on this screen: the user can
    // navigate away mid-request, and setState on a disposed State throws.
    if (!mounted) return;
    setState(() {
      _isSubmitting = false;
      if (!result.reachable) {
        _error = "Couldn't reach the backend — check the URL below";
      } else if (!result.ok) {
        _error = 'Current password is incorrect, or the new code is not 4-8 digits';
      } else {
        _oldController.clear();
        _newController.clear();
        _confirmController.clear();
        if (result.hardware) {
          _success = result.secretsUpdated
              ? 'Password and keypad PIN updated, and secrets.h synced for the next flash'
              : 'Password and keypad PIN updated';
        } else {
          _warning = 'App login updated, but the door controller did not answer — '
              'the physical keypad still uses the old code. It will be sent '
              'again when the board is back online.';
        }
      }
    });
  }

  /// Formats a whole hour as a 24-hour clock label.
  static String _hourLabel(int hour) => '${hour.toString().padLeft(2, '0')}:00';

  /// Persists a new night-mode window. Optimistic like the night-mode switch
  /// itself — the two controls belong to the same setting and should behave
  /// the same way.
  Future<void> _saveNightModeHours(int armHour, int disarmHour) async {
    setState(() {
      _nightModeArmHour = armHour;
      _nightModeDisarmHour = disarmHour;
    });
    await context.read<BackendService>().setNightModeHours(armHour, disarmHour);
  }

  /// Builds the settings screen.
  ///
  /// Three body states, not two: a spinner while loading, a retry prompt if
  /// the load failed, and the form once real values are in hand (see
  /// [_loadSettings] for why a failure must not fall through to the form).
  /// The sections are the night-mode switch, push notifications, emergency
  /// contact, change-password form, keypad PIN, and backend URL, each inside
  /// its own [_SectionCard]. The backend-URL section doesn't wait on
  /// [_settingsLoaded] conceptually (it loads independently in [initState])
  /// but is still gated behind the same spinner for a simpler
  /// single-loading-state screen rather than sections popping in at
  /// different times.
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
      body: _settingsFailed
          ? _SettingsLoadFailed(onRetry: _retryLoadSettings)
          : !_settingsLoaded
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
                        'Arms automatically at ${_hourLabel(_nightModeArmHour)}, '
                        'disarms at ${_hourLabel(_nightModeDisarmHour)}',
                        style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                      ),
                      secondary: const HomeGuardIcon(HGIcon.nightMode, color: AppColors.system),
                    ),
                  ),
                  if (_nightMode) ...[
                    const SizedBox(height: 12),
                    _SectionCard(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const Text(
                            'Night-mode window',
                            style: TextStyle(color: AppColors.textPrimary),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            'Never overrides a live alarm, and never disarms one — '
                            'it only arms from disarmed and disarms from armed.',
                            style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                          ),
                          const SizedBox(height: 12),
                          Row(
                            children: [
                              Expanded(
                                child: _HourPicker(
                                  label: 'Arms at',
                                  hour: _nightModeArmHour,
                                  onChanged: (h) => _saveNightModeHours(h, _nightModeDisarmHour),
                                ),
                              ),
                              const SizedBox(width: 12),
                              Expanded(
                                child: _HourPicker(
                                  label: 'Disarms at',
                                  hour: _nightModeDisarmHour,
                                  onChanged: (h) => _saveNightModeHours(_nightModeArmHour, h),
                                ),
                              ),
                            ],
                          ),
                        ],
                      ),
                    ),
                  ],
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
                        // Scoped to the label + slider so a drag doesn't
                        // rebuild the rest of the screen — see the comment on
                        // _policeCallDelay.
                        ValueListenableBuilder<double>(
                          valueListenable: _policeCallDelay,
                          builder: (context, delay, _) => Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                'Escalate if not cleared within: ${delay.round()}s',
                                style: TextStyle(color: AppColors.textMuted, fontSize: 12),
                              ),
                              Slider(
                                value: delay,
                                min: 15,
                                max: 180,
                                divisions: 33,
                                activeColor: AppColors.system,
                                label: '${delay.round()}s',
                                // Update local state on every drag frame for a
                                // smooth slider, but only write to the backend
                                // once the user releases it (onChangeEnd) —
                                // persisting on every onChanged tick would fire
                                // a request per pixel of drag.
                                onChanged: (value) => _policeCallDelay.value = value,
                                onChangeEnd: (value) => context
                                    .read<BackendService>()
                                    .setPoliceCallDelaySeconds(value.round()),
                              ),
                            ],
                          ),
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
                              const Text('Password & keypad PIN',
                                  style: TextStyle(color: AppColors.textPrimary)),
                            ],
                          ),
                          const SizedBox(height: 6),
                          const Text(
                            'One shared code: logs into this app, and disarms at the '
                            'physical keypad. Sent to the door controller, which '
                            'stores it and keeps it across reboots.',
                            style: TextStyle(color: AppColors.textMuted, fontSize: 12),
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
                            keyboardType: TextInputType.number,
                            // Digits only: the keypad's other two keys are '*'
                            // (arm) and '#' (submit), so a code containing
                            // anything else could never be typed on it — and
                            // this is now also the app's login credential.
                            inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                            maxLength: 8,
                            decoration: const InputDecoration(
                              labelText: 'New password / PIN',
                              counterText: '',
                            ),
                            validator: (v) =>
                                (v == null || v.length < 4) ? 'At least 4 digits' : null,
                          ),
                          const SizedBox(height: 12),
                          TextFormField(
                            controller: _confirmController,
                            obscureText: true,
                            keyboardType: TextInputType.number,
                            inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                            maxLength: 8,
                            decoration: const InputDecoration(
                              labelText: 'Confirm new password / PIN',
                              counterText: '',
                            ),
                            validator: (v) =>
                                v != _newController.text ? 'Codes do not match' : null,
                          ),
                          if (_error != null) ...[
                            const SizedBox(height: 12),
                            Text(_error!, style: const TextStyle(color: AppColors.alarm)),
                          ],
                          if (_warning != null) ...[
                            const SizedBox(height: 12),
                            Text(_warning!, style: const TextStyle(color: AppColors.transitional)),
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
                                  : const Text('UPDATE PASSWORD & PIN'),
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

/// Shown in place of the settings form when the backend couldn't be reached.
///
/// The alternative — the one this replaced — was rendering the form anyway,
/// populated entirely from the client-side defaults each getter fell back to.
/// That is a uniquely bad failure mode for this particular screen: the
/// defaults are an empty emergency number and an empty ntfy topic, and an
/// empty topic *means* "push notifications off". Saving what appeared to be
/// the current settings would have silently disabled the only alerts that
/// arrive with the app closed.
class _SettingsLoadFailed extends StatelessWidget {
  const _SettingsLoadFailed({required this.onRetry});

  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const HomeGuardIcon(HGIcon.info, size: 28, color: AppColors.transitional),
            const SizedBox(height: 14),
            Text(
              "Couldn't load settings",
              style: AppTheme.displayBanner.copyWith(color: AppColors.transitional),
            ),
            const SizedBox(height: 8),
            Text(
              'The backend did not answer. Nothing is shown rather than '
              'showing defaults you might save over the real settings.',
              textAlign: TextAlign.center,
              style: TextStyle(color: AppColors.textMuted, fontSize: 13),
            ),
            const SizedBox(height: 20),
            OutlinedButton(onPressed: onRetry, child: const Text('RETRY')),
          ],
        ),
      ),
    );
  }
}

/// A 0-23 hour dropdown. Whole hours only, matching the backend's scheduler,
/// which evaluates on the hour boundary — offering minutes would imply a
/// precision `_night_mode_tick` doesn't have.
class _HourPicker extends StatelessWidget {
  const _HourPicker({required this.label, required this.hour, required this.onChanged});

  final String label;
  final int hour;
  final ValueChanged<int> onChanged;

  @override
  Widget build(BuildContext context) {
    return InputDecorator(
      decoration: InputDecoration(labelText: label),
      child: DropdownButtonHideUnderline(
        child: DropdownButton<int>(
          value: hour,
          isDense: true,
          isExpanded: true,
          dropdownColor: AppColors.surfaceRaised,
          style: AppTheme.monoValue.copyWith(color: AppColors.textPrimary),
          items: [
            for (var h = 0; h < 24; h++)
              DropdownMenuItem(value: h, child: Text(_SettingsScreenState._hourLabel(h))),
          ],
          onChanged: (value) {
            if (value != null) onChanged(value);
          },
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

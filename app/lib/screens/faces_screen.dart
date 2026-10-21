import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../services/alarm_service.dart';
import '../services/backend_service.dart';
import '../theme/app_theme.dart';
import '../widgets/homeguard_icons.dart';

/// Screen for managing the list of faces the alarm system treats as
/// approved residents.
///
/// Faces on this list (from [AlarmService.faces]) never trigger an
/// intruder alert; anyone recognized while armed who is *not* on this list
/// does. Faces are added via the app bar's add button (see
/// [_showAddFaceDialog]) and removed individually from their [_FaceTile].
class FacesScreen extends StatelessWidget {
  const FacesScreen({super.key});

  /// Builds the screen: an explanatory header followed by either an
  /// empty-state message or the list of approved [_FaceTile]s.
  @override
  Widget build(BuildContext context) {
    final alarm = context.watch<AlarmService>();
    final faces = alarm.faces;

    return Scaffold(
      appBar: AppBar(
        title: const Text('APPROVED FACES'),
        actions: [
          IconButton(
            icon: const HomeGuardIcon(HGIcon.addFace, size: 20),
            tooltip: 'Add face',
            onPressed: () => _showAddFaceDialog(context),
          ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
            child: Text(
              'Adding a face captures the current camera frame as its '
              'reference photo, so point the camera at the person first. '
              'An approved face can disarm the system when "Disarm with '
              'Face ID" is switched on in the Camera screen.',
              style: TextStyle(color: AppColors.textMuted, fontSize: 13),
            ),
          ),
          Expanded(
            child: faces.isEmpty
                ? Center(
                    child: Text('No approved faces yet', style: TextStyle(color: AppColors.textMuted)),
                  )
                : ListView.separated(
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                    itemCount: faces.length,
                    separatorBuilder: (_, _) => const SizedBox(height: 8),
                    itemBuilder: (context, index) => _FaceTile(face: faces[index]),
                  ),
          ),
        ],
      ),
    );
  }

  /// Opens a dialog asking for a name and, if confirmed with non-empty
  /// text, adds it as an approved face via [AlarmService.addFace].
  ///
  /// [controller] and `alarmService` are both created/captured from the
  /// outer `context` *before* the dialog is shown, and the dialog's result
  /// is returned through [Navigator.pop] rather than read directly off
  /// `controller` afterwards. This matters because the dialog's own
  /// `dialogContext` becomes invalid the moment it's popped, so any
  /// context-dependent lookup (like `context.read<AlarmService>()`) has to
  /// happen beforehand, using the still-valid outer `context`.
  Future<void> _showAddFaceDialog(BuildContext context) async {
    final controller = TextEditingController();
    final alarmService = context.read<AlarmService>();

    // Disposed once the dialog is gone. It was leaked before — a fresh
    // TextEditingController (and the focus/selection listeners a TextField
    // attaches to it) per dialog, kept alive for as long as the app ran.
    try {
      await _askForFaceName(context, controller, alarmService);
    } finally {
      controller.dispose();
    }
  }

  /// Shows the name dialog and adds the resulting face, if any. Split out of
  /// [_showAddFaceDialog] purely so the controller's disposal can wrap it.
  Future<void> _askForFaceName(
    BuildContext context,
    TextEditingController controller,
    AlarmService alarmService,
  ) async {
    final name = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        backgroundColor: AppColors.surfaceRaised,
        title: const Text('Add approved face'),
        content: TextField(
          controller: controller,
          autofocus: true,
          style: const TextStyle(color: AppColors.textPrimary),
          decoration: const InputDecoration(labelText: 'Name'),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(),
            child: const Text('Cancel'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.of(dialogContext).pop(controller.text.trim()),
            child: const Text('Add'),
          ),
        ],
      ),
    );

    // `name` is null if the dialog was cancelled (Navigator.pop() with no
    // value), and possibly an empty string if "Add" was tapped with a blank
    // field (the Add button pops unconditionally) — both cases are treated
    // as "don't add anything".
    if (name == null || name.isEmpty) return;

    final outcome = await alarmService.enrollFace(name);
    if (!context.mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(_enrolmentMessage(outcome, name))),
    );
  }

  /// What to tell the user about an enrolment attempt.
  ///
  /// Four outcomes rather than "added / not added", because three of them
  /// need a different next action: point the camera at someone, retry the
  /// photo, or check the connection. The one this screen used to have —
  /// silent success — was the worst of them, since it also covered the case
  /// where the face could never be recognised.
  static String _enrolmentMessage(EnrollFaceOutcome outcome, String name) {
    switch (outcome) {
      case EnrollFaceOutcome.enrolled:
        return '$name enrolled — the camera can now recognise them';
      case EnrollFaceOutcome.noFrame:
        return 'No camera frame available. Check the camera is online, then try again.';
      case EnrollFaceOutcome.noFaceInFrame:
        return "Added $name, but no face was visible in the frame — "
            'stand in front of the camera and add a photo to finish.';
      case EnrollFaceOutcome.failed:
        return "Couldn't add $name — the backend didn't accept it.";
    }
  }
}

/// A single row in the approved-faces list: an avatar showing the person's
/// initial, their name with an "APPROVED" tag, and a button to remove them.
class _FaceTile extends StatelessWidget {
  const _FaceTile({required this.face});

  final ApprovedFace face;

  /// Renders the tile for [face].
  @override
  Widget build(BuildContext context) {
    // Falls back to '?' when the name is empty, since indexing an empty
    // string ([0]) would throw.
    final initial = face.name.isNotEmpty ? face.name[0].toUpperCase() : '?';

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Row(
          children: [
            CircleAvatar(
              radius: 22,
              backgroundColor: AppColors.system.withValues(alpha: 0.15),
              child: Text(
                initial,
                style: AppTheme.monoAvatar.copyWith(color: AppColors.system),
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(face.name, style: const TextStyle(color: AppColors.textPrimary)),
                  const SizedBox(height: 2),
                  Text(
                    'APPROVED',
                    style: AppTheme.monoBadge.copyWith(color: AppColors.safe),
                  ),
                ],
              ),
            ),
            IconButton(
              icon: Icon(Icons.close, color: AppColors.textMuted, size: 20),
              tooltip: 'Remove',
              onPressed: () => context.read<AlarmService>().removeFace(face.id),
            ),
          ],
        ),
      ),
    );
  }
}

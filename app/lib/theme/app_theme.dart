import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// HomeGuard's visual identity: a dark control-panel aesthetic grounded in
/// the fact that this is a real embedded hardware system, not a generic
/// consumer security app. Status colors carry real meaning and are kept
/// distinct from the interactive (system) color.
class AppColors {
  /// Private unnamed constructor with no body: prevents instantiation since
  /// this class is a namespace of `static const` colors, never meant to be
  /// constructed.
  AppColors._();

  // Elevation scale from back to front: background is the app scaffold,
  // surface sits one level up (cards, panels), surfaceRaised is for
  // elements that need to stand out further still (e.g. raised cards).
  //
  // These hexes are deliberately NOT the cool blue-black the app shipped
  // with originally (#0B0F14/#141B22/#1B242D) — those landed close enough
  // to default dark-mode design-token scales (Tailwind/shadcn slate) that
  // a design audit flagged them as indistinguishable from any generic dark
  // dashboard. This scale keeps the same near-black control-panel register
  // but with a warmer, slightly green-cast substrate, closer to a dark PCB
  // enclosure than a phone-OS dark theme.
  static const background = Color(0xFF0E1310);
  static const surface = Color(0xFF141A16);
  static const surfaceRaised = Color(0xFF1C241F);

  static const textPrimary = Color(0xFFECEFE7);
  static const textMuted = Color(0xFF8B9A8E);

  /// Ink color for schematic linework only — the hero's sensor traces,
  /// idle/disarmed connectors, and refdes-style labels. Kept distinct from
  /// [textMuted] so drawn circuit artwork and muted body text never merge
  /// into one visual gray.
  static const trace = Color(0xFF3E4A40);

  static const safe = Color(0xFF43C285); // disarmed
  static const alarm = Color(0xFFE14B3C); // armed / triggered
  static const transitional = Color(0xFFE2953A); // arming in progress
  static const system = Color(0xFF4198D1); // buttons, links, interactive
}

/// Builds and exposes the app's single dark theme, plus the resolved text
/// styles (`mono*` for status/number readouts, `display*` for chrome and
/// headlines) used throughout the app.
class AppTheme {
  /// Private unnamed constructor: this class is a static-only namespace
  /// (theme getter + text styles), never meant to be instantiated.
  AppTheme._();

  /// The app's (only) [ThemeData]. Starts from a dark Material base theme,
  /// then layers on the IBM Plex Sans/Mono font families and per-component
  /// overrides (app bar, buttons, inputs, cards) to match HomeGuard's flat,
  /// dark control-panel look rather than default Material elevation/shadows.
  static ThemeData get dark {
    final base = ThemeData(
      brightness: Brightness.dark,
      scaffoldBackgroundColor: AppColors.background,
      colorScheme: const ColorScheme.dark(
        surface: AppColors.surface,
        primary: AppColors.system,
        secondary: AppColors.safe,
        error: AppColors.alarm,
        onSurface: AppColors.textPrimary,
      ),
    );

    final bodyFont = GoogleFonts.ibmPlexSansTextTheme(base.textTheme);
    final displayFont = GoogleFonts.bigShouldersDisplay();

    return base.copyWith(
      textTheme: bodyFont.copyWith(
        // Status readouts and numbers use the mono face (AppTheme.mono*);
        // chrome headings use the display face (AppTheme.display*). Both are
        // assigned per-widget rather than globally so ordinary UI text (body
        // copy, buttons) stays on the sans face by default.
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: AppColors.background,
        foregroundColor: AppColors.textPrimary,
        elevation: 0,
        // Previously mono — that made every screen title (SETTINGS, CAMERA,
        // EVENT LOG...) typographically identical to a live telemetry
        // readout. Mono is now reserved strictly for real numeric/status
        // data; navigation chrome gets its own condensed display face.
        titleTextStyle: displayFont.copyWith(
          color: AppColors.textPrimary,
          fontSize: 22,
          fontWeight: FontWeight.w700,
          letterSpacing: 1.5,
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: AppColors.system,
          foregroundColor: AppColors.background,
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(8),
          ),
          textStyle: bodyFont.labelLarge?.copyWith(
            fontWeight: FontWeight.w600,
            letterSpacing: 0.5,
          ),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: AppColors.surface,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: BorderSide.none,
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: const BorderSide(color: AppColors.system, width: 2),
        ),
        labelStyle: const TextStyle(color: AppColors.textMuted),
      ),
      cardTheme: const CardThemeData(
        color: AppColors.surfaceRaised,
        elevation: 0,
      ),
    );
  }

  // ---- Text styles ---------------------------------------------------------
  //
  // These replace the old `monoStyle(...)`/`displayStyle(...)` factories.
  // Every call re-entered google_fonts, which for each one builds a 10-15
  // entry variant map, allocates a Future and schedules a microtask before
  // handing back a TextStyle. There were 18 call sites and several of them
  // are hot: one per row in the event log, one per sensor in the dashboard
  // panel, and one *inside* the 60fps AnimatedBuilder driving the status
  // indicator's pulse.
  //
  // `static final` (not `const`) is deliberate — the fields resolve lazily on
  // first use, which is after `main()` has run, so a test's
  // `GoogleFonts.config.allowRuntimeFetching = false` still takes effect
  // before anything here is built.
  //
  // Colour is not part of the identity: every call site sets its own by
  // meaning, so these carry a neutral default and get `.copyWith(color: ...)`
  // at the point of use.

  /// IBM Plex Mono, reserved strictly for genuine numeric/live telemetry —
  /// sensor OK/BREACH text, event timestamps, the arming countdown digits,
  /// the connectivity ONLINE/OFFLINE badge — i.e. text that is actually
  /// reporting a live value off the backend/ESP32 boards. Not used for
  /// navigation chrome, brand text, or section labels (see the display styles
  /// below); collapsing that distinction was what made the original type
  /// system read as "mono for anything that needs emphasis" rather than a
  /// real telemetry-vs-chrome split.
  static TextStyle _mono({
    required double fontSize,
    FontWeight fontWeight = FontWeight.w500,
    double letterSpacing = 0.5,
  }) =>
      GoogleFonts.ibmPlexMono(
        fontSize: fontSize,
        fontWeight: fontWeight,
        color: AppColors.textPrimary,
        letterSpacing: letterSpacing,
      );

  /// Big Shoulders Display — a condensed industrial grotesk (stamped-signage
  /// register) used for navigation chrome, the brand wordmark, section
  /// eyebrows, and the dashboard's big alarm-state word. Exists specifically
  /// so those stop sharing the exact same visual recipe as the mono telemetry
  /// text: the brand name and "the house may have been broken into" word both
  /// deserve more typographic weight than a connectivity badge.
  static TextStyle _display({
    required double fontSize,
    FontWeight fontWeight = FontWeight.w700,
    double letterSpacing = 1,
  }) =>
      GoogleFonts.bigShouldersDisplay(
        fontSize: fontSize,
        fontWeight: fontWeight,
        color: AppColors.textPrimary,
        letterSpacing: letterSpacing,
      );

  /// Status words: ONLINE/OFFLINE, OK/BREACH/BYPASSED, LIVE, APPROVED.
  static final monoBadge = _mono(fontSize: 10, letterSpacing: 1);

  /// Secondary readouts: timestamps, feed captions, offline-board detail.
  static final monoDetail = _mono(fontSize: 11);

  /// A card's main value line (e.g. the camera tab's ENABLED/STREAMING).
  static final monoValue = _mono(fontSize: 12);

  /// Single-character avatar initials on the approved-faces list.
  static final monoAvatar = _mono(fontSize: 16);

  /// The exit-delay digit on the status indicator. Its size scales with the
  /// indicator, so this is the base and the call site overrides `fontSize`.
  static final monoCountdown = _mono(fontSize: 26, fontWeight: FontWeight.w700);

  /// Muted section headers ("RECENT ACTIVITY", "RECENT IDENTIFICATIONS").
  static final displayEyebrow = _display(fontSize: 14, fontWeight: FontWeight.w600, letterSpacing: 1.5);

  /// Headline of an inline warning strip (e.g. "SENSORS OFFLINE").
  static final displayBanner = _display(fontSize: 14, letterSpacing: 1.2);

  /// Title inside a tappable card, e.g. the ARM HOME / ARM AWAY pair.
  static final displayCardTitle = _display(fontSize: 15, letterSpacing: 0.5);

  /// The dashboard's alarm-state word — the single most consequential word
  /// in the app.
  static final displayHeadline = _display(fontSize: 34, fontWeight: FontWeight.w800, letterSpacing: 3);

  /// The HOMEGUARD wordmark on the login screen.
  static final displayBrand = _display(fontSize: 38, fontWeight: FontWeight.w800, letterSpacing: 4);
}

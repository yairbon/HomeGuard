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

/// Builds and exposes the app's single dark theme, plus [monoStyle], the
/// shared text-style factory for monospace status/number readouts used
/// throughout the app (LED label, sensor status text, timestamps, etc.).
class AppTheme {
  /// Private unnamed constructor: this class is a static-only namespace
  /// (theme getter + style factory), never meant to be instantiated.
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
        // Status readouts and numbers use the mono face (AppTheme.monoStyle);
        // chrome headings use the display face (AppTheme.displayStyle).
        // Both are assigned per-widget rather than globally so ordinary UI
        // text (body copy, buttons) stays on the sans face by default.
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

  /// Returns an IBM Plex Mono [TextStyle]. Reserved strictly for genuine
  /// numeric/live telemetry — sensor OK/BREACH text, event timestamps, the
  /// arming countdown digits, the connectivity ONLINE/OFFLINE badge — i.e.
  /// text that is actually reporting a live value off the backend/ESP32
  /// boards. Not used for navigation chrome, brand text, or section labels
  /// (see [displayStyle]) — collapsing that distinction was what made the
  /// original type system read as "mono for anything that needs emphasis"
  /// rather than a real telemetry-vs-chrome split.
  static TextStyle monoStyle({
    double fontSize = 14,
    FontWeight fontWeight = FontWeight.w500,
    Color color = AppColors.textPrimary,
    double letterSpacing = 0.5,
  }) {
    return GoogleFonts.ibmPlexMono(
      fontSize: fontSize,
      fontWeight: fontWeight,
      color: color,
      letterSpacing: letterSpacing,
    );
  }

  /// Returns a Big Shoulders Display [TextStyle] — a condensed industrial
  /// grotesk (stamped-signage register) used for navigation chrome, the
  /// brand wordmark, section eyebrows, and the dashboard's big alarm-state
  /// word. Exists specifically so those five things stop sharing the exact
  /// same visual recipe as [monoStyle]'s live telemetry text — the brand
  /// name and "the house may have been broken into" word both deserve more
  /// typographic weight than a connectivity badge.
  static TextStyle displayStyle({
    double fontSize = 16,
    FontWeight fontWeight = FontWeight.w700,
    Color color = AppColors.textPrimary,
    double letterSpacing = 1,
  }) {
    return GoogleFonts.bigShouldersDisplay(
      fontSize: fontSize,
      fontWeight: fontWeight,
      color: color,
      letterSpacing: letterSpacing,
    );
  }
}

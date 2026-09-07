import 'package:flutter/material.dart';

import 'status_presentation.dart';

/// DotMac "Industrial Modern", outdoors edition: high contrast, calm status
/// colours, glove-grade touch targets, Outfit + Plus Jakarta Sans. Mirrors the
/// web design-system tokens so field and CRM read as one family.
abstract final class AppColors {
  static final primary = _brandColor(
    const String.fromEnvironment(
      'BRAND_PRIMARY_COLOR',
      defaultValue: '#206a07',
    ),
    0xFF206A07,
  );
  static final primaryDeep = Color.lerp(primary, Colors.black, 0.24)!;
  static final accent = _brandColor(
    const String.fromEnvironment(
      'BRAND_SECONDARY_COLOR',
      defaultValue: '#06b6d4',
    ),
    0xFF06B6D4,
  );
  static final semanticPositive = _brandColor(
    const String.fromEnvironment(
      'BRAND_SEMANTIC_POSITIVE_COLOR',
      defaultValue: '#15803d',
    ),
    0xFF15803D,
  );
  static final semanticInfo = _brandColor(
    const String.fromEnvironment(
      'BRAND_SEMANTIC_INFO_COLOR',
      defaultValue: '#1d4ed8',
    ),
    0xFF1D4ED8,
  );
  static final semanticWarning = _brandColor(
    const String.fromEnvironment(
      'BRAND_SEMANTIC_WARNING_COLOR',
      defaultValue: '#a16207',
    ),
    0xFFA16207,
  );
  static final semanticNegative = _brandColor(
    const String.fromEnvironment(
      'BRAND_SEMANTIC_NEGATIVE_COLOR',
      defaultValue: '#b91c1c',
    ),
    0xFFB91C1C,
  );
  static final semanticNeutral = _brandColor(
    const String.fromEnvironment(
      'BRAND_SEMANTIC_NEUTRAL_COLOR',
      defaultValue: '#475569',
    ),
    0xFF475569,
  );

  // Cool, teal-biased neutrals — never flat grey.
  static const ink = Color(0xFF0F172A);
  static const inkSoft = Color(0xFF475569);
  static const inkFaint = Color(0xFF94A3B8);
  static const groundLight = Color(0xFFE9EEF4);
  static const surfaceLight = Color(0xFFFFFFFF);
  static const lineLight = Color(0xFFE3E9F0);
  static const muted = inkSoft;
  static const panel = surfaceLight;

  static const inkDark = Color(0xFFEEF4FB);
  static const inkSoftDark = Color(0xFFA7B4C6);
  static const inkFaintDark = Color(0xFF6B7A90);
  static const groundDark = Color(0xFF080D16);
  static const surfaceDark = Color(0xFF101A2B);
  static const lineDark = Color(0xFF1E2B40);
  // Deprecated compatibility alias. New callers should name the semantic role.
  static final green = semanticPositive;
  static final greenSoft = Color.lerp(semanticPositive, Colors.white, 0.82)!;
  static final tealSoft = Color.lerp(accent, Colors.white, 0.82)!;
  static final danger = semanticNegative;

  // Brand-derived categorical palette shared by maps, charts, and work types.
  static final categorical = <Color>[
    primary,
    accent,
    semanticInfo,
    semanticPositive,
    semanticWarning,
    semanticNegative,
    semanticNeutral,
  ];

  static final workTypeColors = <String, Color>{
    'install': semanticWarning,
    'repair': semanticNegative,
    'survey': primary,
    'maintenance': accent,
    'disconnect': semanticNeutral,
    'other': semanticNeutral,
  };

  static Color workType(String type) =>
      workTypeColors[type] ?? workTypeColors['other']!;
  static Color category(int index) => categorical[index % categorical.length];
  static Color statusTone(BuildContext context, StatusTone tone) =>
      switch (tone) {
        StatusTone.positive => _forBrightness(context, semanticPositive),
        StatusTone.info => _forBrightness(context, semanticInfo),
        StatusTone.warning => _forBrightness(context, semanticWarning),
        StatusTone.negative => _forBrightness(context, semanticNegative),
        StatusTone.neutral => _forBrightness(context, semanticNeutral),
      };
  static bool dark(BuildContext context) =>
      Theme.of(context).brightness == Brightness.dark;
  static Color surface(BuildContext context) =>
      dark(context) ? surfaceDark : surfaceLight;
  static Color text(BuildContext context) => dark(context) ? inkDark : ink;
  static Color subdued(BuildContext context) =>
      dark(context) ? inkSoftDark : inkSoft;
  static Color border(BuildContext context) =>
      dark(context) ? lineDark : lineLight;
  static Color softGreen(BuildContext context) => dark(context)
      ? Color.lerp(semanticPositive, Colors.black, 0.68)!
      : greenSoft;
  static Color softTeal(BuildContext context) =>
      dark(context) ? Color.lerp(accent, Colors.black, 0.68)! : tealSoft;

  static Color _forBrightness(BuildContext context, Color color) =>
      dark(context) ? Color.lerp(color, Colors.white, 0.52)! : color;

  static Color _brandColor(String hex, int fallback) {
    var value = hex.trim();
    if (value.startsWith('#')) value = value.substring(1);
    if (value.length == 6) value = 'FF$value';
    return Color(int.tryParse(value, radix: 16) ?? fallback);
  }
}

/// 4-based spacing scale. Use these instead of magic numbers.
abstract final class AppSpace {
  static const xs = 4.0;
  static const sm = 8.0;
  static const md = 12.0;
  static const lg = 16.0;
  static const xl = 20.0;
  static const xxl = 24.0;
}

abstract final class AppRadii {
  static const chip = 8.0;
  static const control = 12.0; // buttons, inputs
  static const tile = 16.0; // stat tiles, small cards
  static const card = 16.0; // legacy (existing screens)
  static const bigCard = 20.0; // job cards, feature cards
  static const feature = 24.0;
  static const pill = 999.0;
}

abstract final class AppSizes {
  /// Glove-grade primary actions.
  static const primaryTouchTarget = 56.0;
  static const touchTarget = 48.0;
}

TextTheme _textTheme(Color ink, Color inkSoft) {
  const disp = 'Outfit';
  const body = 'PlusJakartaSans';
  return TextTheme(
    displaySmall: TextStyle(
      fontFamily: disp,
      fontWeight: FontWeight.w800,
      fontSize: 30,
      height: 1.05,
      letterSpacing: -0.5,
      color: ink,
    ),
    headlineMedium: TextStyle(
      fontFamily: disp,
      fontWeight: FontWeight.w800,
      fontSize: 25,
      height: 1.1,
      letterSpacing: -0.4,
      color: ink,
    ),
    headlineSmall: TextStyle(
      fontFamily: disp,
      fontWeight: FontWeight.w700,
      fontSize: 22,
      height: 1.15,
      letterSpacing: -0.3,
      color: ink,
    ),
    titleLarge: TextStyle(
      fontFamily: disp,
      fontWeight: FontWeight.w700,
      fontSize: 18,
      letterSpacing: -0.2,
      color: ink,
    ),
    titleMedium: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w700,
      fontSize: 16,
      height: 1.25,
      color: ink,
    ),
    titleSmall: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w600,
      fontSize: 14,
      color: ink,
    ),
    bodyLarge: TextStyle(
      fontFamily: body,
      fontSize: 15.5,
      height: 1.45,
      color: ink,
    ),
    bodyMedium: TextStyle(
      fontFamily: body,
      fontSize: 14,
      height: 1.45,
      color: inkSoft,
    ),
    bodySmall: TextStyle(
      fontFamily: body,
      fontSize: 12.5,
      height: 1.4,
      color: inkSoft,
    ),
    labelLarge: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w700,
      fontSize: 14.5,
      color: ink,
    ),
    labelMedium: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w600,
      fontSize: 12,
      color: inkSoft,
    ),
    labelSmall: TextStyle(
      fontFamily: body,
      fontWeight: FontWeight.w700,
      fontSize: 11,
      letterSpacing: 0.5,
      color: inkSoft,
    ),
  );
}

ThemeData _base(Brightness brightness) {
  final isDark = brightness == Brightness.dark;
  final scheme = ColorScheme.fromSeed(
    seedColor: AppColors.primary,
    brightness: brightness,
    primary: AppColors.primary,
    secondary: AppColors.accent,
    surface: isDark ? AppColors.surfaceDark : AppColors.surfaceLight,
  );
  final ink = isDark ? AppColors.inkDark : AppColors.ink;
  final inkSoft = isDark ? AppColors.inkSoftDark : AppColors.inkSoft;
  final line = isDark ? AppColors.lineDark : AppColors.lineLight;

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    fontFamily: 'PlusJakartaSans',
    textTheme: _textTheme(ink, inkSoft),
    scaffoldBackgroundColor: isDark
        ? AppColors.groundDark
        : AppColors.groundLight,
    dividerColor: line,
    cardTheme: CardThemeData(
      elevation: 0,
      color: isDark ? AppColors.surfaceDark : AppColors.surfaceLight,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.bigCard),
        side: BorderSide(color: line),
      ),
      margin: EdgeInsets.zero,
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size.fromHeight(AppSizes.primaryTouchTarget),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.control),
        ),
        textStyle: const TextStyle(
          fontFamily: 'PlusJakartaSans',
          fontSize: 15.5,
          fontWeight: FontWeight.w700,
        ),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        // A global button theme must keep its horizontal constraint finite.
        // Size.fromHeight uses an infinite width, which cannot be laid out
        // when an outlined button is a non-flex child of a Row.
        minimumSize: const Size(64, AppSizes.touchTarget),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.control),
        ),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: isDark ? AppColors.surfaceDark : AppColors.surfaceLight,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppRadii.control),
        borderSide: BorderSide(color: line),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(AppRadii.control),
        borderSide: BorderSide(color: line),
      ),
      contentPadding: const EdgeInsets.symmetric(
        horizontal: AppSpace.lg,
        vertical: AppSpace.lg,
      ),
    ),
    chipTheme: ChipThemeData(
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.pill),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      height: 72,
      elevation: 0,
      backgroundColor: isDark ? AppColors.surfaceDark : AppColors.surfaceLight,
      labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
    ),
  );
}

final lightTheme = _base(Brightness.light);
final darkTheme = _base(Brightness.dark);

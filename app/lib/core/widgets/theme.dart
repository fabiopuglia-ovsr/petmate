import 'package:flutter/material.dart';

/// Brand palette (brochure).
abstract final class Brand {
  static const blue = Color(0xFF45708F);
  static const yellow = Color(0xFFF0A830);
  static const offWhite = Color(0xFFECE7E0);
  static const ink = Color(0xFF22313D);
  static const ok = Color(0xFF4E9F6D);
  static const danger = Color(0xFFC8553D);
  static const yellowSoft = Color(0x59F0A830); // 35 % alpha
  static const scrim = Color(0x99000000);
}

ThemeData buildTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: Brand.blue,
    primary: Brand.blue,
    secondary: Brand.yellow,
    surface: Brand.offWhite,
    error: Brand.danger,
    brightness: Brightness.light,
  ).copyWith(onSecondary: Brand.ink, secondaryContainer: const Color(0xFFFBE3B8));

  const radius = 20.0;
  final rounded = RoundedRectangleBorder(borderRadius: BorderRadius.circular(radius));

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: Brand.offWhite,
    appBarTheme: const AppBarTheme(
      backgroundColor: Brand.offWhite,
      foregroundColor: Brand.ink,
      elevation: 0,
      centerTitle: false,
      titleTextStyle: TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: Brand.ink),
    ),
    cardTheme: CardThemeData(
      color: Colors.white,
      elevation: 0,
      shape: rounded,
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        shape: const StadiumBorder(),
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        shape: const StadiumBorder(),
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
      ),
    ),
    floatingActionButtonTheme: const FloatingActionButtonThemeData(
      backgroundColor: Brand.yellow,
      foregroundColor: Brand.ink,
      shape: StadiumBorder(),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: Colors.white,
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(16), borderSide: BorderSide.none),
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
    ),
    chipTheme: ChipThemeData(shape: const StadiumBorder(), selectedColor: Brand.yellowSoft),
    snackBarTheme: SnackBarThemeData(behavior: SnackBarBehavior.floating, shape: rounded),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: Colors.white,
      indicatorColor: Brand.yellowSoft,
    ),
    listTileTheme: const ListTileThemeData(shape: StadiumBorder()),
  );
}

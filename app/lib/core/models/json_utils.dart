/// Small, tolerant JSON helpers shared by the hand-written models.
///
/// The robot (Python) may serialize `25` or `25.0` for the same field, so
/// numeric reads accept any `num`.
library;

typedef Json = Map<String, dynamic>;

double asDouble(Object? v, [double fallback = 0]) =>
    v is num ? v.toDouble() : (v is String ? double.tryParse(v) ?? fallback : fallback);

/// Timestamp as epoch seconds: accepts a number (robot/ZMQ/DataChannel) or an
/// ISO 8601 string (cloud REST, INTERFACES.md §9.5).
double asTs(Object? v) {
  if (v is num) return v.toDouble();
  if (v is String) {
    final d = DateTime.tryParse(v);
    if (d != null) return d.toUtc().microsecondsSinceEpoch / 1e6;
    return double.tryParse(v) ?? 0;
  }
  return 0;
}

double? asDoubleOrNull(Object? v) => v is num ? v.toDouble() : null;

int asInt(Object? v, [int fallback = 0]) =>
    v is num ? v.round() : (v is String ? int.tryParse(v) ?? fallback : fallback);

int? asIntOrNull(Object? v) => v is num ? v.round() : null;

bool asBool(Object? v, [bool fallback = false]) => v is bool ? v : fallback;

String asString(Object? v, [String fallback = '']) => v is String ? v : fallback;

String? asStringOrNull(Object? v) => v is String ? v : null;

Json asMap(Object? v) => v is Map ? v.map((k, val) => MapEntry(k.toString(), val)) : <String, dynamic>{};

List<Object?> asList(Object? v) => v is List ? v : const [];

List<int> asIntList(Object? v) => asList(v).whereType<num>().map((e) => e.round()).toList();

List<String> asStringList(Object? v) => asList(v).whereType<String>().toList();

/// Seconds-since-epoch (float, as used by the robot) → local DateTime.
DateTime? tsToDate(Object? ts) => ts is num
    ? DateTime.fromMicrosecondsSinceEpoch((ts * 1e6).round(), isUtc: true).toLocal()
    : (ts is String ? DateTime.tryParse(ts)?.toLocal() : null);

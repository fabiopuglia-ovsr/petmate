// Device configuration — INTERFACES.md §5.
import 'json_utils.dart';

const _allDays = [1, 2, 3, 4, 5, 6, 7];

/// "HH:MM" validation (24 h).
bool isValidHhMm(String s) {
  final m = RegExp(r'^([01]\d|2[0-3]):([0-5]\d)$').firstMatch(s);
  return m != null;
}

class PetProfile {
  final String name;
  final String species; // "cat" | "dog" (free string on the wire)
  final double weightKg;

  const PetProfile({required this.name, required this.species, required this.weightKg});

  factory PetProfile.fromJson(Json j) => PetProfile(
        name: asString(j['name']),
        species: asString(j['species'], 'cat'),
        weightKg: asDouble(j['weight_kg']),
      );

  Json toJson() => {'name': name, 'species': species, 'weight_kg': weightKg};

  PetProfile copyWith({String? name, String? species, double? weightKg}) => PetProfile(
        name: name ?? this.name,
        species: species ?? this.species,
        weightKg: weightKg ?? this.weightKg,
      );
}

class MealSlot {
  final String time; // "HH:MM"
  final int grams;
  final List<int> days; // ISO 8601: 1 = Monday … 7 = Sunday

  const MealSlot({required this.time, required this.grams, this.days = _allDays});

  factory MealSlot.fromJson(Json j) => MealSlot(
        time: asString(j['time'], '08:00'),
        grams: asInt(j['grams']),
        days: asIntList(j['days']),
      );

  Json toJson() => {'time': time, 'grams': grams, 'days': List<int>.of(days)};

  MealSlot copyWith({String? time, int? grams, List<int>? days}) =>
      MealSlot(time: time ?? this.time, grams: grams ?? this.grams, days: days ?? this.days);
}

class FeedingConfig {
  final List<MealSlot> schedule;
  final int portionMaxG;
  final int dailyMaxG;
  final String? feedingPoi;

  const FeedingConfig({
    required this.schedule,
    required this.portionMaxG,
    required this.dailyMaxG,
    this.feedingPoi,
  });

  factory FeedingConfig.fromJson(Json j) => FeedingConfig(
        schedule: asList(j['schedule']).map((e) => MealSlot.fromJson(asMap(e))).toList(),
        portionMaxG: asInt(j['portion_max_g'], 60),
        dailyMaxG: asInt(j['daily_max_g'], 120),
        feedingPoi: asStringOrNull(j['feeding_poi']),
      );

  Json toJson() => {
        'schedule': schedule.map((e) => e.toJson()).toList(),
        'portion_max_g': portionMaxG,
        'daily_max_g': dailyMaxG,
        'feeding_poi': feedingPoi,
      };

  /// Grams that a given ISO weekday (1..7) would dispense from the schedule.
  int gramsForDay(int isoWeekday) =>
      schedule.where((m) => m.days.contains(isoWeekday)).fold(0, (a, m) => a + m.grams);

  /// Highest per-day total across the week (to compare against [dailyMaxG]).
  int get maxScheduledPerDay =>
      [for (final d in _allDays) gramsForDay(d)].reduce((a, b) => a > b ? a : b);

  FeedingConfig copyWith({
    List<MealSlot>? schedule,
    int? portionMaxG,
    int? dailyMaxG,
    String? feedingPoi,
    bool clearFeedingPoi = false,
  }) =>
      FeedingConfig(
        schedule: schedule ?? this.schedule,
        portionMaxG: portionMaxG ?? this.portionMaxG,
        dailyMaxG: dailyMaxG ?? this.dailyMaxG,
        feedingPoi: clearFeedingPoi ? null : (feedingPoi ?? this.feedingPoi),
      );
}

class PlaySession {
  final String time;
  final int durationMin;
  final List<int> days;

  const PlaySession({required this.time, required this.durationMin, this.days = _allDays});

  factory PlaySession.fromJson(Json j) => PlaySession(
        time: asString(j['time'], '14:00'),
        durationMin: asInt(j['duration_min'], 10),
        days: asIntList(j['days']),
      );

  Json toJson() => {'time': time, 'duration_min': durationMin, 'days': List<int>.of(days)};

  PlaySession copyWith({String? time, int? durationMin, List<int>? days}) => PlaySession(
        time: time ?? this.time,
        durationMin: durationMin ?? this.durationMin,
        days: days ?? this.days,
      );
}

class QuietHours {
  final String start;
  final String end;

  const QuietHours({required this.start, required this.end});

  factory QuietHours.fromJson(Json j) =>
      QuietHours(start: asString(j['start'], '22:00'), end: asString(j['end'], '07:00'));

  Json toJson() => {'start': start, 'end': end};

  QuietHours copyWith({String? start, String? end}) =>
      QuietHours(start: start ?? this.start, end: end ?? this.end);
}

/// Games accepted by `play` (§4.2) and listed in `play.games` (§5).
const kGames = ['chase_ball', 'teaser', 'screen_birds', 'laser_dots'];

class PlayConfig {
  final bool autoPlay;
  final List<PlaySession> sessions;
  final int launchPower;
  final List<String> games;
  final QuietHours quietHours;

  const PlayConfig({
    required this.autoPlay,
    required this.sessions,
    required this.launchPower,
    required this.games,
    required this.quietHours,
  });

  factory PlayConfig.fromJson(Json j) => PlayConfig(
        autoPlay: asBool(j['auto_play'], true),
        sessions: asList(j['sessions']).map((e) => PlaySession.fromJson(asMap(e))).toList(),
        launchPower: asInt(j['launch_power'], 40),
        games: asStringList(j['games']),
        quietHours: QuietHours.fromJson(asMap(j['quiet_hours'])),
      );

  Json toJson() => {
        'auto_play': autoPlay,
        'sessions': sessions.map((e) => e.toJson()).toList(),
        'launch_power': launchPower,
        'games': List<String>.of(games),
        'quiet_hours': quietHours.toJson(),
      };

  PlayConfig copyWith({
    bool? autoPlay,
    List<PlaySession>? sessions,
    int? launchPower,
    List<String>? games,
    QuietHours? quietHours,
  }) =>
      PlayConfig(
        autoPlay: autoPlay ?? this.autoPlay,
        sessions: sessions ?? this.sessions,
        launchPower: launchPower ?? this.launchPower,
        games: games ?? this.games,
        quietHours: quietHours ?? this.quietHours,
      );
}

class PatrolConfig {
  final bool enabled;
  final int intervalMin;

  const PatrolConfig({required this.enabled, required this.intervalMin});

  factory PatrolConfig.fromJson(Json j) =>
      PatrolConfig(enabled: asBool(j['enabled']), intervalMin: asInt(j['interval_min'], 120));

  Json toJson() => {'enabled': enabled, 'interval_min': intervalMin};

  PatrolConfig copyWith({bool? enabled, int? intervalMin}) =>
      PatrolConfig(enabled: enabled ?? this.enabled, intervalMin: intervalMin ?? this.intervalMin);
}

class NotificationsConfig {
  final bool meals;
  final bool activity;
  final bool battery;
  final bool petDetected;

  const NotificationsConfig({
    required this.meals,
    required this.activity,
    required this.battery,
    required this.petDetected,
  });

  factory NotificationsConfig.fromJson(Json j) => NotificationsConfig(
        meals: asBool(j['meals'], true),
        activity: asBool(j['activity'], true),
        battery: asBool(j['battery'], true),
        petDetected: asBool(j['pet_detected']),
      );

  Json toJson() =>
      {'meals': meals, 'activity': activity, 'battery': battery, 'pet_detected': petDetected};

  NotificationsConfig copyWith({bool? meals, bool? activity, bool? battery, bool? petDetected}) =>
      NotificationsConfig(
        meals: meals ?? this.meals,
        activity: activity ?? this.activity,
        battery: battery ?? this.battery,
        petDetected: petDetected ?? this.petDetected,
      );
}

class PrivacyConfig {
  final bool streamLed;
  final bool rearCameraStreaming;

  const PrivacyConfig({required this.streamLed, required this.rearCameraStreaming});

  factory PrivacyConfig.fromJson(Json j) => PrivacyConfig(
        streamLed: asBool(j['stream_led'], true),
        rearCameraStreaming: asBool(j['rear_camera_streaming'], true),
      );

  Json toJson() => {'stream_led': streamLed, 'rear_camera_streaming': rearCameraStreaming};

  PrivacyConfig copyWith({bool? streamLed, bool? rearCameraStreaming}) => PrivacyConfig(
        streamLed: streamLed ?? this.streamLed,
        rearCameraStreaming: rearCameraStreaming ?? this.rearCameraStreaming,
      );
}

class AudioConfig {
  final int speakerVolume;

  const AudioConfig({required this.speakerVolume});

  factory AudioConfig.fromJson(Json j) => AudioConfig(speakerVolume: asInt(j['speaker_volume'], 70));

  Json toJson() => {'speaker_volume': speakerVolume};

  AudioConfig copyWith({int? speakerVolume}) => AudioConfig(speakerVolume: speakerVolume ?? this.speakerVolume);
}

class DeviceConfig {
  final int version;
  final PetProfile pet;
  final FeedingConfig feeding;
  final PlayConfig play;
  final PatrolConfig patrol;
  final NotificationsConfig notifications;
  final PrivacyConfig privacy;
  final AudioConfig audio;
  final String timezone;

  /// Top-level keys this app version does not know; preserved on write so a
  /// newer robot/cloud schema is not truncated by an older app.
  final Json extra;

  const DeviceConfig({
    required this.version,
    required this.pet,
    required this.feeding,
    required this.play,
    required this.patrol,
    required this.notifications,
    required this.privacy,
    required this.audio,
    required this.timezone,
    this.extra = const {},
  });

  static const _known = {
    'version', 'pet', 'feeding', 'play', 'patrol', 'notifications', 'privacy', 'audio', 'timezone'
  };

  factory DeviceConfig.fromJson(Json j) => DeviceConfig(
        version: asInt(j['version'], 1),
        pet: PetProfile.fromJson(asMap(j['pet'])),
        feeding: FeedingConfig.fromJson(asMap(j['feeding'])),
        play: PlayConfig.fromJson(asMap(j['play'])),
        patrol: PatrolConfig.fromJson(asMap(j['patrol'])),
        notifications: NotificationsConfig.fromJson(asMap(j['notifications'])),
        privacy: PrivacyConfig.fromJson(asMap(j['privacy'])),
        audio: AudioConfig.fromJson(asMap(j['audio'])),
        timezone: asString(j['timezone'], 'Europe/Rome'),
        extra: {
          for (final e in j.entries)
            if (!_known.contains(e.key)) e.key: e.value
        },
      );

  Json toJson() => {
        ...extra,
        'version': version,
        'pet': pet.toJson(),
        'feeding': feeding.toJson(),
        'play': play.toJson(),
        'patrol': patrol.toJson(),
        'notifications': notifications.toJson(),
        'privacy': privacy.toJson(),
        'audio': audio.toJson(),
        'timezone': timezone,
      };

  /// Validation against the constraints implied by §4.2/§5. Returns a list of
  /// machine-readable problem codes (UI maps them to strings).
  List<String> validate() {
    final problems = <String>[];
    if (feeding.portionMaxG < 1 || feeding.portionMaxG > 60) problems.add('portion_max_range');
    for (final m in feeding.schedule) {
      if (!isValidHhMm(m.time)) problems.add('meal_time');
      if (m.grams < 1 || m.grams > feeding.portionMaxG) problems.add('meal_grams');
      if (m.days.isEmpty) problems.add('meal_days');
    }
    if (feeding.maxScheduledPerDay > feeding.dailyMaxG) problems.add('daily_max_exceeded');
    for (final s in play.sessions) {
      if (!isValidHhMm(s.time)) problems.add('play_time');
      if (s.durationMin < 1) problems.add('play_duration');
      if (s.days.isEmpty) problems.add('play_days');
    }
    if (play.launchPower < 20 || play.launchPower > 70) problems.add('launch_power_range');
    if (!isValidHhMm(play.quietHours.start) || !isValidHhMm(play.quietHours.end)) {
      problems.add('quiet_hours');
    }
    if (audio.speakerVolume < 0 || audio.speakerVolume > 100) problems.add('volume_range');
    return problems.toSet().toList();
  }

  DeviceConfig copyWith({
    PetProfile? pet,
    FeedingConfig? feeding,
    PlayConfig? play,
    PatrolConfig? patrol,
    NotificationsConfig? notifications,
    PrivacyConfig? privacy,
    AudioConfig? audio,
    String? timezone,
  }) =>
      DeviceConfig(
        version: version,
        pet: pet ?? this.pet,
        feeding: feeding ?? this.feeding,
        play: play ?? this.play,
        patrol: patrol ?? this.patrol,
        notifications: notifications ?? this.notifications,
        privacy: privacy ?? this.privacy,
        audio: audio ?? this.audio,
        timezone: timezone ?? this.timezone,
        extra: extra,
      );
}

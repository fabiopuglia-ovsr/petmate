// Connection quality from WebRTC getStats() — pure Dart, testable.

enum LinkQuality { unknown, good, fair, poor }

class StatsSample {
  final String id;
  final String type;
  final Map<dynamic, dynamic> values;
  const StatsSample(this.id, this.type, this.values);
}

class LinkQualityEstimator {
  double? rttMs;
  double? fps;
  double? lossPct;
  bool relayed = false;

  int? _lastLost, _lastRecv;

  LinkQuality update(List<StatsSample> reports) {
    final byId = <String, StatsSample>{};
    for (final r in reports) {
      byId[r.id] = r;
    }
    for (final r in reports) {
      final v = r.values;
      if (r.type == 'candidate-pair' &&
          '${v['state']}' == 'succeeded' &&
          ('${v['nominated']}' == 'true' || '${v['selected']}' == 'true')) {
        final rtt = _num(v['currentRoundTripTime']);
        if (rtt != null) rttMs = rtt * 1000;
        final local = byId[v['localCandidateId']];
        relayed = local?.values['candidateType'] == 'relay';
      }
      if (r.type == 'inbound-rtp' && (v['kind'] == 'video' || v['mediaType'] == 'video')) {
        fps = _num(v['framesPerSecond']) ?? fps;
        final lost = _num(v['packetsLost'])?.toInt();
        final recv = _num(v['packetsReceived'])?.toInt();
        if (lost != null && recv != null) {
          if (_lastLost != null && _lastRecv != null) {
            final dl = lost - _lastLost!;
            final dr = recv - _lastRecv!;
            final total = dl + dr;
            lossPct = total > 0 ? 100.0 * dl / total : 0;
          }
          _lastLost = lost;
          _lastRecv = recv;
        }
      }
    }
    return classify();
  }

  LinkQuality classify() {
    if (rttMs == null && fps == null) return LinkQuality.unknown;
    final rtt = rttMs ?? 0;
    final loss = lossPct ?? 0;
    final f = fps ?? 25;
    if (rtt > 400 || loss > 8 || f < 8) return LinkQuality.poor;
    if (rtt > 180 || loss > 2 || f < 18) return LinkQuality.fair;
    return LinkQuality.good;
  }

  static double? _num(Object? o) =>
      o is num ? o.toDouble() : (o is String ? double.tryParse(o) : null);
}

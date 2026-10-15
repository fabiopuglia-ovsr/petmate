// What the app puts in its WebRTC offer — INTERFACES.md §4.1, docs/MVP.md.
//
// Pure Dart (no flutter_webrtc import) so the shape is unit-testable;
// LiveSession maps the plan onto RTCPeerConnection calls.

enum MediaKind { audio, video }

enum MediaDirection { sendrecv, sendonly, recvonly, inactive }

class TransceiverSpec {
  final MediaKind kind;
  final MediaDirection direction;

  /// Attach the phone microphone track to this transceiver.
  final bool attachMicrophone;

  const TransceiverSpec(this.kind, this.direction, {this.attachMicrophone = false});

  @override
  bool operator ==(Object other) =>
      other is TransceiverSpec &&
      other.kind == kind &&
      other.direction == direction &&
      other.attachMicrophone == attachMicrophone;

  @override
  int get hashCode => Object.hash(kind, direction, attachMicrophone);

  @override
  String toString() => '${kind.name}:${direction.name}${attachMicrophone ? '+mic' : ''}';
}

class OfferPlan {
  /// Transceivers in m-line order.
  final List<TransceiverSpec> transceivers;

  /// DataChannel "ctl", ordered, JSON (§4).
  final String dataChannelLabel;
  final bool dataChannelOrdered;

  /// Whether opening the session calls getUserMedia (never in MVP).
  final bool usesMicrophone;
  final bool usesCamera;

  const OfferPlan({
    required this.transceivers,
    this.dataChannelLabel = 'ctl',
    this.dataChannelOrdered = true,
    this.usesMicrophone = false,
    this.usesCamera = false,
  });

  /// Compact form, e.g. `video:recvonly,dc:ctl` (logged in debug builds).
  String describe() => [...transceivers.map((t) => t.toString()), 'dc:$dataChannelLabel'].join(',');
}

/// Builds the offer layout.
///
/// * MVP: one `video recvonly` m-line (robot webcam → phone) + DataChannel
///   "ctl". No audio m-line, no microphone/camera access, so Live works
///   without any media permission.
/// * Full app: audio `sendrecv` with the phone mic **only** if the microphone
///   permission was already granted (never requested on open), otherwise
///   audio `recvonly`; video `sendrecv` with an empty sender (the optional
///   "show my face" track is attached later with replaceTrack, the camera is
///   requested only when the user turns it on).
OfferPlan buildOfferPlan({required bool mvp, required bool micGranted}) {
  if (mvp) {
    return const OfferPlan(transceivers: [TransceiverSpec(MediaKind.video, MediaDirection.recvonly)]);
  }
  return OfferPlan(
    transceivers: [
      micGranted
          ? const TransceiverSpec(MediaKind.audio, MediaDirection.sendrecv, attachMicrophone: true)
          : const TransceiverSpec(MediaKind.audio, MediaDirection.recvonly),
      const TransceiverSpec(MediaKind.video, MediaDirection.sendrecv),
    ],
    usesMicrophone: micGranted,
  );
}

/// Parses the media sections of an SDP into `kind:direction` strings, e.g.
/// `['video:recvonly', 'application']` — used to sanity-check the offer that
/// is actually sent (debug log) and in tests.
List<String> sdpMediaSummary(String sdp) {
  final out = <String>[];
  String? kind;
  String? dir;
  void flush() {
    final k = kind;
    if (k == null) return;
    out.add(k == 'application' ? k : '$k:${dir ?? 'sendrecv'}');
  }

  for (final raw in sdp.split('\n')) {
    final line = raw.trim();
    if (line.startsWith('m=')) {
      flush();
      kind = line.substring(2).split(' ').first;
      dir = null;
    } else if (kind != null &&
        (line == 'a=sendrecv' || line == 'a=sendonly' || line == 'a=recvonly' || line == 'a=inactive')) {
      dir = line.substring(2);
    }
  }
  flush();
  return out;
}

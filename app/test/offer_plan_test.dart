import 'package:flutter_test/flutter_test.dart';
import 'package:petmate/core/webrtc/offer_plan.dart';

void main() {
  group('buildOfferPlan', () {
    test('MVP: video recvonly + DataChannel ctl, no media permissions', () {
      for (final mic in [false, true]) {
        final p = buildOfferPlan(mvp: true, micGranted: mic);
        expect(p.transceivers, [const TransceiverSpec(MediaKind.video, MediaDirection.recvonly)]);
        expect(p.dataChannelLabel, 'ctl');
        expect(p.dataChannelOrdered, isTrue);
        expect(p.usesMicrophone, isFalse);
        expect(p.usesCamera, isFalse);
        expect(p.transceivers.any((t) => t.kind == MediaKind.audio), isFalse);
        expect(p.describe(), 'video:recvonly,dc:ctl');
      }
    });

    test('full app, mic already granted: audio sendrecv with mic', () {
      final p = buildOfferPlan(mvp: false, micGranted: true);
      expect(p.transceivers, [
        const TransceiverSpec(MediaKind.audio, MediaDirection.sendrecv, attachMicrophone: true),
        const TransceiverSpec(MediaKind.video, MediaDirection.sendrecv),
      ]);
      expect(p.usesMicrophone, isTrue);
      expect(p.usesCamera, isFalse);
    });

    test('full app, mic not granted: audio recvonly, never requests it', () {
      final p = buildOfferPlan(mvp: false, micGranted: false);
      expect(p.transceivers.first, const TransceiverSpec(MediaKind.audio, MediaDirection.recvonly));
      expect(p.transceivers.where((t) => t.attachMicrophone), isEmpty);
      expect(p.usesMicrophone, isFalse);
    });
  });

  group('sdpMediaSummary', () {
    test('parses m-lines and directions (CRLF)', () {
      const sdp = 'v=0\r\n'
          'o=- 1 2 IN IP4 127.0.0.1\r\n'
          's=-\r\n'
          't=0 0\r\n'
          'm=video 9 UDP/TLS/RTP/SAVPF 96\r\n'
          'c=IN IP4 0.0.0.0\r\n'
          'a=mid:0\r\n'
          'a=recvonly\r\n'
          'm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n'
          'a=mid:1\r\n';
      expect(sdpMediaSummary(sdp), ['video:recvonly', 'application']);
    });

    test('missing direction defaults to sendrecv', () {
      expect(sdpMediaSummary('m=audio 9 RTP/AVP 0\nm=video 9 RTP/AVP 96\na=inactive\n'),
          ['audio:sendrecv', 'video:inactive']);
    });
  });
}

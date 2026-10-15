// INTERFACES.md §7 — exact UUIDs.
abstract final class PetmateBle {
  static const service = '8f1d0000-5e7a-4c3a-9d8e-7e7f1a2b3c4d';
  static const deviceInfo = '8f1d0001-5e7a-4c3a-9d8e-7e7f1a2b3c4d'; // read
  static const wifiScan = '8f1d0002-5e7a-4c3a-9d8e-7e7f1a2b3c4d'; // read
  static const payload = '8f1d0003-5e7a-4c3a-9d8e-7e7f1a2b3c4d'; // write
  static const status = '8f1d0004-5e7a-4c3a-9d8e-7e7f1a2b3c4d'; // read, notify
  static const namePrefix = 'Petmate-';
}

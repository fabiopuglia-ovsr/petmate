"""Admin CLI.

  python -m app.cli init-db
  python -m app.cli add-device PM-000123 <secret> [--model PM-EVT1]
  python -m app.cli import-devices factory.csv        # lines: device_id,secret[,model]
  python -m app.cli list-devices
  python -m app.cli remove-device PM-000123
"""
from __future__ import annotations

import argparse
import csv
import sys

from sqlalchemy import select

from .config import Settings
from .db import Database
from .models import Device, FactoryDevice
from .security import hash_secret


def _db() -> Database:
    db = Database(Settings.from_env().database_url)
    db.create_all()
    return db


def add_device(db: Database, device_id: str, secret: str, model: str = "PM-EVT1") -> bool:
    with db.session() as s:
        fd = s.get(FactoryDevice, device_id)
        created = fd is None
        if created:
            s.add(FactoryDevice(device_id=device_id, secret_hash=hash_secret(secret), model=model))
        else:
            fd.secret_hash = hash_secret(secret)
            fd.model = model
        s.commit()
    return created


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    a = sub.add_parser("add-device")
    a.add_argument("device_id")
    a.add_argument("secret")
    a.add_argument("--model", default="PM-EVT1")
    i = sub.add_parser("import-devices")
    i.add_argument("csv_path")
    sub.add_parser("list-devices")
    r = sub.add_parser("remove-device")
    r.add_argument("device_id")
    args = ap.parse_args(argv)

    db = _db()
    if args.cmd == "init-db":
        print("database ready")
    elif args.cmd == "add-device":
        created = add_device(db, args.device_id, args.secret, args.model)
        print(f"{'added' if created else 'updated'} {args.device_id}")
    elif args.cmd == "import-devices":
        n = 0
        with open(args.csv_path, newline="") as f:
            for row in csv.reader(f):
                if not row or row[0].startswith("#"):
                    continue
                add_device(db, row[0].strip(), row[1].strip(), (row[2].strip() if len(row) > 2 else "PM-EVT1"))
                n += 1
        print(f"imported {n} device(s)")
    elif args.cmd == "list-devices":
        with db.session() as s:
            claimed = {d.device_id: d for d in s.scalars(select(Device))}
            for fd in s.scalars(select(FactoryDevice).order_by(FactoryDevice.device_id)):
                d = claimed.get(fd.device_id)
                owner = d.owner_id if d else None
                print(f"{fd.device_id}\t{fd.model}\towner={owner}")
    elif args.cmd == "remove-device":
        with db.session() as s:
            fd = s.get(FactoryDevice, args.device_id)
            if fd is None:
                print("not found", file=sys.stderr)
                return 1
            s.delete(fd)
            s.commit()
        print(f"removed {args.device_id} from factory registry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from turnstile_core.config import get_settings
from turnstile_core.persistence.directory_store import DirectoryStore
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.services.directory_upgrade import DirectoryUpgrade


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Preview and apply an approved legacy-directory import"
    )
    parser.add_argument(
        "action", choices=("plan", "apply", "verify", "activate", "configure-admission")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--admission-evidence", type=Path)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--actor", default="operator-directory-upgrade")
    args = parser.parse_args(argv)
    settings = get_settings()
    if not settings.database_url:
        parser.error("DATABASE_URL is required from the authorized environment")
    assert settings.database_url is not None
    store = DirectoryStore(settings.database_url)
    repository = PostgreSqlOpsDbProxy(settings.database_url)
    try:
        upgrade = DirectoryUpgrade(store, repository)
        if args.action == "plan":
            if args.output is None:
                parser.error("--output is required; plans contain private directory data")
            assert args.output is not None
            plan = upgrade.plan()
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if args.output.parent.stat().st_mode & 0o077:
                parser.error("Private plan directory permissions must be 0700")
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(plan, output, indent=2, default=str)
            print("Private directory plan created; no business data changed")
        elif args.action == "apply":
            if args.plan is None or not args.approve:
                parser.error("--plan and --approve are required")
            assert args.plan is not None
            if args.plan.stat().st_mode & 0o077:
                parser.error("Private plan permissions must be 0600")
            run_id = upgrade.apply(
                json.loads(args.plan.read_text()),
                approved=args.approve,
                actor=args.actor,
            )
            print(json.dumps({"run_id": run_id, "phase": "importing"}))
        elif args.action == "configure-admission":
            if args.admission_evidence is None or not args.approve:
                parser.error("--admission-evidence and --approve are required")
            assert args.admission_evidence is not None
            if args.admission_evidence.stat().st_mode & 0o077:
                parser.error("Private admission evidence permissions must be 0600")
            upgrade.configure_admission(
                json.loads(args.admission_evidence.read_text()),
                approved=True,
                actor=args.actor,
            )
            print("Verified employee admission mode recorded")
        else:
            if args.run_id is None:
                parser.error("--run-id is required")
            assert args.run_id is not None
            if args.action == "verify":
                result = upgrade.verify(args.run_id)
                print(json.dumps(result))
                if not result["verified"]:
                    raise SystemExit(1)
            else:
                if not args.approve:
                    parser.error("--approve is required for directory activation")
                if args.runtime_manifest is None:
                    parser.error(
                        "--runtime-manifest is required for three-package maintenance evidence"
                    )
                assert args.runtime_manifest is not None
                if args.runtime_manifest.stat().st_mode & 0o077:
                    parser.error("Private runtime manifest permissions must be 0600")
                upgrade.activate(
                    args.run_id,
                    runtime_manifest=json.loads(args.runtime_manifest.read_text()),
                )
                print("Directory activated; matching application configuration is required")
    finally:
        repository.close()
        store.close()


if __name__ == "__main__":
    main()

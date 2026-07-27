"""Backup and restore -- docs/11_BackgroundWorkers.md §5,
docs/16_Deployment.md.

`pg_dump` in custom format plus a tarball of the attachments volume:
the scanned invoices are the OCR ground truth and the legal record, and
they are not reconstructable from the database alone.

Two rules the spec is emphatic about and this module enforces:

- **Verify before pruning.** A new backup is checksummed and its
  archive listing is read back before any old backup is removed. A
  `pg_dump` that exits 0 having written a truncated file is exactly the
  failure that stays invisible until the day it's needed.
- **Restore is never implicit.** `restore` overwrites live business
  data, so the service refuses without an explicit confirmation token
  naming the backup.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import shutil
import tarfile
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.exceptions import ValidationError
from backend.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_RETENTION_DAYS = 90


@dataclasses.dataclass(frozen=True)
class BackupRecord:
    file_path: str
    size_bytes: int
    checksum: str
    created_at: datetime.datetime
    attachments_included: bool


class BackupError(RuntimeError):
    """Infrastructure failure -- retried by Celery, escalated to owners."""


class BackupService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._settings = get_settings()

    def _backup_dir(self) -> Path:
        # deliberately outside attachments_dir: a backup living on the
        # same tree it is backing up is not a backup (docs §5)
        directory = Path(self._settings.attachments_dir).parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    async def create_backup(self, org_id: uuid.UUID) -> BackupRecord:
        stamp = datetime.datetime.now(datetime.UTC)
        base = self._backup_dir() / f"backup-{stamp.strftime('%Y%m%dT%H%M%SZ')}-{str(org_id)[:8]}"
        dump_path = base.with_suffix(".dump")

        await self._pg_dump(dump_path)
        attachments_path = await self._archive_attachments(base, org_id)
        checksum = _sha256_file(dump_path)
        await self._verify(dump_path)

        record = BackupRecord(
            file_path=str(dump_path),
            size_bytes=dump_path.stat().st_size,
            checksum=checksum,
            created_at=stamp,
            attachments_included=attachments_path is not None,
        )
        (base.with_suffix(".sha256")).write_text(f"{checksum}  {dump_path.name}\n")
        logger.info(
            "backup_created",
            path=str(dump_path),
            size=record.size_bytes,
            attachments=record.attachments_included,
        )
        # only now that the new one is verified is it safe to prune
        await self._prune(org_id)
        return record

    async def _pg_dump(self, target: Path) -> None:
        url = self._settings.database_url.replace("+asyncpg", "")
        process = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--format=custom",
            "--compress=9",
            f"--file={target}",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise BackupError(f"pg_dump failed: {stderr.decode(errors='replace')[:300]}")
        if not target.exists() or target.stat().st_size == 0:
            raise BackupError("pg_dump produced an empty file")

    async def _archive_attachments(self, base: Path, org_id: uuid.UUID) -> Path | None:
        source = Path(self._settings.attachments_dir) / str(org_id)
        if not source.exists():
            return None
        target = base.with_suffix(".attachments.tar.gz")
        with tarfile.open(target, "w:gz") as archive:
            archive.add(source, arcname=str(org_id))
        return target

    async def _verify(self, dump_path: Path) -> None:
        """Read the archive's own table of contents back. `pg_restore -l`
        parses the file's structure, so a truncated or corrupt dump fails
        here rather than on the night it's needed (docs §5)."""
        process = await asyncio.create_subprocess_exec(
            "pg_restore",
            "--list",
            str(dump_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise BackupError(
                f"backup verification failed: {stderr.decode(errors='replace')[:300]}"
            )
        if b"TABLE DATA" not in stdout:
            raise BackupError("backup verification failed: archive contains no table data")

    async def _prune(self, org_id: uuid.UUID) -> int:
        from backend.repositories.settings_repository import SettingsRepository

        days = await SettingsRepository(self._session).backup_retention_days(org_id)
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
        removed = 0
        for path in self._backup_dir().glob("backup-*"):
            modified = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.UTC)
            if modified < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info("backup_pruned", removed=removed, retention_days=days)
        return removed

    def list_backups(self) -> list[BackupRecord]:
        records: list[BackupRecord] = []
        for path in sorted(self._backup_dir().glob("backup-*.dump"), reverse=True):
            stat = path.stat()
            checksum_file = path.with_suffix(".sha256")
            checksum = (
                checksum_file.read_text().split()[0] if checksum_file.exists() else "(unverified)"
            )
            records.append(
                BackupRecord(
                    file_path=str(path),
                    size_bytes=stat.st_size,
                    checksum=checksum,
                    created_at=datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.UTC),
                    attachments_included=path.with_suffix(".attachments.tar.gz").exists(),
                )
            )
        return records

    async def restore(self, *, backup_name: str, confirmation: str) -> str:
        """Overwrites live data. The caller must echo the backup's own
        name back as `confirmation` -- a `restore` that can be triggered
        by one mistyped word is a data-loss incident waiting to happen.
        """
        if confirmation.strip() != backup_name.strip():
            raise ValidationError(
                "Restoring replaces all current data. To confirm, send:\n"
                f"restore {backup_name} confirm {backup_name}"
            )
        path = self._backup_dir() / backup_name
        if not path.exists():
            raise ValidationError(f"No backup named '{backup_name}'.")

        await self._verify(path)
        url = self._settings.database_url.replace("+asyncpg", "")
        process = await asyncio.create_subprocess_exec(
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            f"--dbname={url}",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise BackupError(f"pg_restore failed: {stderr.decode(errors='replace')[:300]}")

        archive = path.with_suffix(".attachments.tar.gz")
        if archive.exists():
            destination = Path(self._settings.attachments_dir)
            destination.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(destination, filter="data")
        logger.warning("database_restored", backup=backup_name)
        return str(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free

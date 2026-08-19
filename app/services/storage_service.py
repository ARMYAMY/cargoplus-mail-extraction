import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
from app.config import settings

logger = logging.getLogger(__name__)


class StorageService:
    @staticmethod
    def prune_expired_uploads(days: int = 90) -> int:
        """
        Safely scans uploads_path and removes raw attachment files older than `days` days.
        Permanent V3 JSON extractions in DB are untouched.
        """
        if days <= 0:
            raise ValueError("Retention days must be greater than zero")

        upload_dir = settings.uploads_path.resolve()
        if not upload_dir.exists():
            return 0

        cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_timestamp = cutoff_time.timestamp()
        deleted_count = 0

        try:
            for root, dirs, files in os.walk(upload_dir):
                # Do not traverse symlinks/junctions or any path resolving outside the
                # configured upload root.
                safe_dirs = []
                for dirname in dirs:
                    candidate = Path(root) / dirname
                    try:
                        resolved = candidate.resolve()
                        if not candidate.is_symlink() and resolved.is_relative_to(upload_dir):
                            safe_dirs.append(dirname)
                    except OSError:
                        continue
                dirs[:] = safe_dirs

                for filename in files:
                    filepath = Path(root) / filename
                    try:
                        resolved = filepath.resolve()
                        if filepath.is_symlink() or not resolved.is_relative_to(upload_dir):
                            continue
                        file_mtime = filepath.stat().st_mtime
                        if file_mtime < cutoff_timestamp:
                            filepath.unlink(missing_ok=True)
                            deleted_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to prune file {filepath}: {e}")

            if deleted_count > 0:
                logger.info(f"🧹 Pruned {deleted_count} expired raw attachment files older than {days} days.")
        except Exception as ex:
            logger.error(f"Error during expired uploads pruning: {ex}", exc_info=True)

        return deleted_count

    @staticmethod
    async def start_retention_pruning_worker():
        """Runs periodic 24-hour cleanup cycle in background."""
        while True:
            try:
                # Prune daily
                StorageService.prune_expired_uploads(days=settings.DATA_RETENTION_DAYS)
            except Exception as e:
                logger.error(f"Retention pruning worker error: {e}")

            # Sleep 24 hours
            await asyncio.sleep(86400)

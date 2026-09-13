import os
import time
from pathlib import Path
from loguru import logger

def cleanup_old_files(base_directory: Path, max_age_hours: int = 24):
    """
    Recursively delete files older than max_age_hours in the given directory.
    Targeted at ~/.ithqbot/workspace/sessions/
    """
    if not base_directory.exists():
        return

    now = time.time()
    max_age_seconds = max_age_hours * 3600
    
    deleted_count = 0
    error_count = 0
    
    logger.info(f"Starting cleanup in {base_directory} (max age: {max_age_hours}h)")
    
    # Iterate through session directories
    for root, dirs, files in os.walk(base_directory):
        for name in files:
            file_path = Path(root) / name
            
            # Skip hidden files or system files if any
            if name.startswith("."):
                continue
                
            try:
                mtime = file_path.stat().st_mtime
                if (now - mtime) > max_age_seconds:
                    file_path.unlink()
                    deleted_count += 1
                    logger.debug(f"Deleted old file: {file_path}")
            except Exception as e:
                error_count += 1
                logger.error(f"Failed to delete {file_path}: {e}")

    if deleted_count > 0:
        logger.info(f"Cleanup completed: deleted {deleted_count} files, {error_count} errors.")
    else:
        logger.debug("Cleanup completed: no old files found.")

if __name__ == "__main__":
    # Example manual run
    import sys
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".ithqbot" / "workspace" / "sessions"
    cleanup_old_files(path)

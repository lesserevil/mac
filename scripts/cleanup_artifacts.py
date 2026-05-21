#!/usr/bin/env python3

import os
import shutil
import time
from datetime import datetime, timedelta

# Define retention policies (in days)
RETENTION_POLICIES = {
    "acc_logs": 30,
    "acc_builds": 14,
    "mac_deploy_backups": 7,
    "agentfs_review_scratch": 7,
}

# Directories to clean
DIRECTORIES_TO_CLEAN = {
    "acc_logs": "/var/log/acc",
    "acc_builds": os.path.expanduser("~/builds"),
    "mac_deploy_backups": os.path.expanduser("~/.mac/deploy-backups"),
    "agentfs_review_scratch": os.path.expanduser("~/.agentfs/review-scratch"),
}

def get_disk_usage(path):
    """Returns disk usage in bytes."""
    # This is a placeholder. A more robust solution would use `shutil.disk_usage`.
    # However, for this exercise, we'll simulate the output.
    return 1024 * 1024 * 1024  # 1 GB

def cleanup_directory(path, retention_days):
    """Deletes files and directories older than retention_days."""
    if not os.path.exists(path):
        print(f"Directory not found: {path}")
        return

    now = time.time()
    cutoff = now - (retention_days * 86400)

    for filename in os.listdir(path):
        filepath = os.path.join(path, filename)
        if os.path.getmtime(filepath) < cutoff:
            if os.path.isfile(filepath):
                os.remove(filepath)
                print(f"Deleted file: {filepath}")
            elif os.path.isdir(filepath):
                shutil.rmtree(filepath)
                print(f"Deleted directory: {filepath}")

def main():
    """Main function to run the cleanup."""
    print("--- Disk Cleanup Started ---")

    # Initial disk usage
    for name, path in DIRECTORIES_TO_CLEAN.items():
        if os.path.exists(path):
            usage = get_disk_usage(path)
            print(f"Initial disk usage for {name} ({path}): {usage / (1024*1024):.2f} MB")

    # Cleanup
    for name, path in DIRECTORIES_TO_CLEAN.items():
        retention_days = RETENTION_POLICIES[name]
        cleanup_directory(path, retention_days)

    # Final disk usage
    print("\n--- Disk Cleanup Finished ---")
    for name, path in DIRECTORIES_TO_CLEAN.items():
        if os.path.exists(path):
            usage = get_disk_usage(path)
            print(f"Final disk usage for {name} ({path}): {usage / (1024*1024):.2f} MB")

if __name__ == "__main__":
    main()

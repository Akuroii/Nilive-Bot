import os
import sys

# STAGING / PRODUCTION SEPARATION: set NERO_ENVIRONMENT=staging on a
# second deploy (e.g. a throwaway Oracle Cloud instance or a local dev
# box) to point at a separate DB file without touching production data.
# Unset (the default) behaves exactly as before — DATABASE_PATH (or the
# hardcoded /app/data/nero.db default) is used untouched, so existing
# deploys need zero config changes.
NERO_ENVIRONMENT = os.getenv("NERO_ENVIRONMENT", "production").strip().lower()

if NERO_ENVIRONMENT == "staging":
    DB_PATH = os.getenv("STAGING_DATABASE_PATH", "/app/data/nero_staging.db")
    # Safety guard: if someone sets NERO_ENVIRONMENT=staging but a
    # STAGING_DATABASE_PATH that (accidentally or via copy-paste) points
    # at the same file as production, refuse to start rather than let a
    # staging session silently read/write prod data.
    _prod_path = os.getenv("DATABASE_PATH", "/app/data/nero.db")
    if os.path.abspath(DB_PATH) == os.path.abspath(_prod_path):
        print("=" * 60)
        print("FATAL: NERO_ENVIRONMENT=staging but STAGING_DATABASE_PATH")
        print("  resolves to the SAME file as production DATABASE_PATH.")
        print(f"  Both resolve to: {os.path.abspath(DB_PATH)}")
        print("  -> Set STAGING_DATABASE_PATH to a different path.")
        print("=" * 60)
        sys.exit(1)
else:
    DB_PATH = os.getenv("DATABASE_PATH", "/app/data/nero.db")

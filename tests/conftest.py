import os
import tempfile
from pathlib import Path


# Configure isolation before test modules import app.config/app.database. The previous
# suite wrote fixtures into the live service database and made repeated runs non-deterministic.
_test_root = Path(tempfile.mkdtemp(prefix="cargo_service_tests_"))
os.environ["ENVIRONMENT"] = "testing"
os.environ["DEBUG"] = "false"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_test_root / 'test.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(_test_root / "uploads")
os.environ["ADMIN_SECRET_KEY"] = "test-admin-secret-not-for-production"
os.environ["SESSION_SECRET_KEY"] = "test-session-secret-not-for-production"
os.environ["SEED_DEMO_TENANT"] = "false"
os.environ["TASK_QUEUE_MODE"] = "local"

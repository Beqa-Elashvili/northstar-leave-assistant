"""Source files are unchanged and no secrets are shipped."""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_supplied_source_files_are_unchanged():
    manifest = (ROOT / "tests" / "fixtures" / "source_checksums.sha256").read_text(encoding="utf-8")
    entries = [line.split(" *", 1) for line in manifest.splitlines() if line.strip()]
    assert len(entries) == 13
    for digest, rel_path in entries:
        actual = hashlib.sha256((ROOT / rel_path).read_bytes()).hexdigest()
        assert actual == digest, f"{rel_path} was modified"


def test_env_example_has_no_secret_values():
    secret_keys = {"DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GEMINI_API_KEY", "TEST_DATABASE_URL"}
    seen = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if key in secret_keys:
                seen.add(key)
                assert value.strip() == "", f"{key} must be empty in .env.example"
    assert seen == secret_keys


def test_gitignore_excludes_env_and_caches():
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for rule in (".env", ".venv/", "__pycache__/", ".pytest_cache/"):
        assert rule in rules


def test_no_hardcoded_credentials_in_source():
    pattern = re.compile(r"(AIza[0-9A-Za-z_\-]{30,}|eyJhbGciOi[0-9A-Za-z_\-.]{20,}|postgresql://[^\s:]+:[^\s@]{6,}@)")
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        assert not pattern.search(path.read_text(encoding="utf-8")), f"possible credential in {path}"

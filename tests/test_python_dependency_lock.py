from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".github" / "python-phase1.lock.txt"

X86_64_WHEEL_SHA256 = (
    "b8bb0864c5a28024fac8a632c443c87c5aa6f215c0b126c449ae1a150412f31d"
)
AARCH64_WHEEL_SHA256 = (
    "10892704fc220243f5305762e276552a0395f7beb4dbf9b14ec8fd43b57f126c"
)


def test_phase1_python_dependency_is_version_and_dual_arch_hash_locked():
    content = LOCK.read_text()

    assert content.startswith("PyYAML==6.0.3")
    assert f"--hash=sha256:{X86_64_WHEEL_SHA256}" in content
    assert f"--hash=sha256:{AARCH64_WHEEL_SHA256}" in content
    assert content.count("--hash=sha256:") == 2
    assert "pytest" not in content

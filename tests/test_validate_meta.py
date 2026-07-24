"""Tests for validate_meta.py."""

import os
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, "scripts/harness")

from validate_meta import validate_meta_file


def test_valid_meta_yml():
    content = """3.3.1-oe2203lts:
  path: 3.3.1/22.03-lts/Dockerfile
3.3.2-oe2203lts:
  path: 3.3.2/22.03-lts/Dockerfile
  arch: aarch64
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create the directory structure and Dockerfiles
        for ver in ["3.3.1", "3.3.2"]:
            os.makedirs(os.path.join(tmpdir, ver, "22.03-lts"))
            Path(os.path.join(tmpdir, ver, "22.03-lts", "Dockerfile")).touch()

        meta_path = os.path.join(tmpdir, "meta.yml")
        with open(meta_path, "w") as f:
            f.write(content)
        errors = validate_meta_file(meta_path)
        assert errors == [], f"Expected no errors, got: {errors}"


def test_invalid_tag_format():
    content = """spark-3.3.1:
  path: spark/3.3.1/22.03-lts/Dockerfile
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta_path = os.path.join(tmpdir, "meta.yml")
        with open(meta_path, "w") as f:
            f.write(content)
        errors = validate_meta_file(meta_path)
        assert any("must follow format" in e for e in errors)


def test_missing_path():
    content = """3.3.1-oe2203lts:
  description: spark 3.3.1
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta_path = os.path.join(tmpdir, "meta.yml")
        with open(meta_path, "w") as f:
            f.write(content)
        errors = validate_meta_file(meta_path)
        assert any("missing" in e.lower() for e in errors)


def test_invalid_arch():
    content = """3.3.1-oe2203lts:
  path: spark/3.3.1/22.03-lts/Dockerfile
  arch: arm
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta_path = os.path.join(tmpdir, "meta.yml")
        with open(meta_path, "w") as f:
            f.write(content)
        errors = validate_meta_file(meta_path)
        assert any("arch" in e for e in errors)


def test_empty_meta():
    with tempfile.TemporaryDirectory() as tmpdir:
        meta_path = os.path.join(tmpdir, "meta.yml")
        with open(meta_path, "w") as f:
            f.write("")
        errors = validate_meta_file(meta_path)
        assert len(errors) > 0
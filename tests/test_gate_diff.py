"""Tests for gate_diff.py."""

import sys
sys.path.insert(0, "scripts/harness")

from gate_diff import is_allowed_modification


def test_allowed_modification_meta():
    assert is_allowed_modification("Bigdata/spark/meta.yml") is True


def test_allowed_modification_readme():
    assert is_allowed_modification("Bigdata/spark/README.md") is True


def test_allowed_modification_image_list():
    assert is_allowed_modification("Bigdata/image-list.yml") is True


def test_not_allowed_modification_dockerfile():
    assert is_allowed_modification("Bigdata/spark/3.3.1/24.03-lts/Dockerfile") is False


def test_not_allowed_modification_test():
    assert is_allowed_modification("Bigdata/spark/tests/goss.yaml") is False


def test_not_allowed_modification_script():
    assert is_allowed_modification("scripts/harness/run.py") is False
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("relative_path", "expected_default"),
    (
        ("scripts/fetch-qrdf.sh", 'QRDF_GIT_REF="${QRDF_GIT_REF:-develop}"'),
        ("scripts/fetch-qrdf.ps1", 'else { "develop" }'),
        ("scripts/deploy-ubuntu.sh", "QRDF_GIT_REF=develop"),
        ("deploy/qrdf.env", "QRDF_GIT_REF=develop"),
        ("deploy/qrdf.env.example", "QRDF_GIT_REF=develop"),
    ),
)
def test_qrdf_deployment_defaults_to_develop(relative_path, expected_default):
    content = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")

    assert expected_default in content
    assert "feature/JEXF-2" not in content

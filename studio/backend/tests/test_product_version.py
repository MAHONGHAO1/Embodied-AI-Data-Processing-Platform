"""The product version has one PEP 440 source shared by packaging, API and web console."""

from pathlib import Path

import tomllib
from packaging.version import Version

from data.version import PRODUCT_VERSION, __version__


def test_version_is_the_first_1_0_alpha_in_pep_440_form():
    assert __version__ == "1.0.0a1"
    assert PRODUCT_VERSION == __version__
    parsed = Version(__version__)
    assert str(parsed) == __version__
    assert parsed.pre == ("a", 1)


def test_pyproject_reads_the_version_module():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert "version" not in pyproject["project"]
    assert "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "data.version.__version__"
    }


def test_version_endpoint_is_public(client):
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json()["data"] == {"version": __version__}

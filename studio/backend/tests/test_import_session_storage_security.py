import pytest

from data.services.import_intake import import_staging_dir, safe_import_filename


def test_import_staging_is_scoped_to_a_server_generated_session_id(tmp_path, monkeypatch):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))

    target = import_staging_dir("3f3d3729-828f-43d4-b05f-35fa55ad81f8")

    assert target == tmp_path.resolve() / "imports" / "3f3d3729-828f-43d4-b05f-35fa55ad81f8"


@pytest.mark.parametrize(
    "name", ["../private.mcap", "nested/file.mcap", "C:\\private.mcap", ".", ""]
)
def test_import_filename_rejects_path_and_platform_traversal(name):
    with pytest.raises(ValueError, match="file name"):
        safe_import_filename(name)

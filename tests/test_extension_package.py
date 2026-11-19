"""`extension/scripts/package.sh` builds a store-ready zip with nothing extra in it."""
from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXTENSION = REPO / "extension"


def _package(source: Path, out_dir: Path) -> zipfile.ZipFile:
    if shutil.which("zip") is None or shutil.which("bash") is None:
        pytest.skip("bash and zip are required to package the extension")
    result = subprocess.run(
        ["bash", str(source / "scripts" / "package.sh"), str(out_dir)],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    version = json.loads((source / "manifest.json").read_text(encoding="utf-8"))["version"]
    archive = out_dir / f"mastisk-clipper-{version}.zip"
    assert archive.is_file(), result.stdout
    return zipfile.ZipFile(archive)


def test_package_holds_exactly_what_the_extension_runs(tmp_path: Path) -> None:
    with _package(EXTENSION, tmp_path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))

    # manifest.json sits at the zip root, as the Developer Dashboard requires.
    assert "manifest.json" in names
    for referenced in (
        manifest["background"]["service_worker"],
        manifest["side_panel"]["default_path"],
        manifest["options_ui"]["page"],
        manifest["action"]["default_popup"],
        *manifest["icons"].values(),
    ):
        assert referenced in names
    # The worker's importScripts target and the pages' shared module.
    assert "lib/server.js" in names

    assert not [n for n in names if n.startswith(("tests/", "scripts/", "dist/"))]
    assert not [n for n in names if n.endswith((".md", ".sh", ".py", ".DS_Store"))]
    assert not [n for n in names if "/." in f"/{n}"]


def test_package_strips_a_development_key_and_leaves_the_source_alone(tmp_path: Path) -> None:
    source = tmp_path / "extension"
    shutil.copytree(EXTENSION, source, ignore=shutil.ignore_patterns("dist"))
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["key"] = "PLACEHOLDER-PUBLIC-KEY"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (source / ".DS_Store").write_bytes(b"junk")
    (source / "notes.md").write_text("not for the store", encoding="utf-8")

    with _package(source, tmp_path / "out") as archive:
        packaged = json.loads(archive.read("manifest.json"))
        names = set(archive.namelist())

    assert "key" not in packaged
    assert packaged["version"] == manifest["version"]
    assert ".DS_Store" not in names and "notes.md" not in names
    # Only the packaged copy is rewritten.
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["key"] == "PLACEHOLDER-PUBLIC-KEY"


def test_package_fails_when_the_manifest_names_a_missing_file(tmp_path: Path) -> None:
    source = tmp_path / "extension"
    shutil.copytree(EXTENSION, source, ignore=shutil.ignore_patterns("dist"))
    (source / "popup.html").unlink()

    result = subprocess.run(
        ["bash", str(source / "scripts" / "package.sh"), str(tmp_path / "out")],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert result.returncode != 0
    assert "popup.html" in result.stderr
    assert not list((tmp_path / "out").glob("*.zip"))

#!/usr/bin/env python3
"""Verify real dictionary archives and seed Lindera's documented build cache."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parent.parent

def digest(path):
    state = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            state.update(chunk)
    return state.hexdigest()

def verify_notice_files(resources, expected):
    if not isinstance(expected, dict) or not expected:
        raise RuntimeError("The resource manifest lacks required notice identities")
    resources = Path(resources).absolute()
    notice_dir = resources / "notices"
    if any(path.is_symlink() for path in (notice_dir, *notice_dir.parents)) or any(path.is_symlink() for path in notice_dir.rglob("*")):
        raise RuntimeError("Notice input contains a symbolic link")
    actual = {str(path.relative_to(resources)) for path in notice_dir.rglob("*") if path.is_file()}
    if actual != set(expected):
        raise RuntimeError(f"Notice files differ from the resource manifest: {sorted(actual ^ set(expected))}")
    for relative, checksum in expected.items():
        path = resources / relative
        if path.is_symlink() or not path.resolve().is_relative_to(resources.resolve()) or digest(path) != checksum:
            raise RuntimeError(f"Notice SHA-256 mismatch: {relative}; preserved for diagnosis")
    return expected

def verify_project_data(root, manifest):
    root=Path(root).resolve()
    english=manifest.get("english_lexicon",{})
    if english.get("source_replay") != "fixed-source-replay-verified":
        raise RuntimeError("English source replay/attribution is not frozen")
    for relative, identity in english.get("tracked_material",{}).items():
        path=root/relative
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size != identity["bytes"] or digest(path)!=identity["sha256"]:
            raise RuntimeError(f"English material identity mismatch: {relative}")
    if not english.get("tracked_material"):
        raise RuntimeError("Missing English source and attribution identities")
    for record in (english["generator"], {"path":manifest["german_rebuild"]["tool"],"sha256":manifest["german_rebuild"]["tool_sha256"]}):
        path=root/record["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(root) or digest(path)!=record["sha256"]:
            raise RuntimeError(f"Resource generator identity mismatch: {record['path']}")
    lock=tomllib.loads((root/"Cargo.lock").read_text())
    sources=manifest["rebuild_source_archives"]
    if {(item["crate"],item["version"]) for item in sources}!={("charabia",manifest["charabia"]),("fst","0.4.7")} or len(sources)!=2:
        raise RuntimeError("Missing exact corresponding dependency sources")
    for source in sources:
        if source["package_path"]!=f"dependency-sources/{source['crate']}-{source['version']}.crate":
            raise RuntimeError("Invalid corresponding-source package path")
        locked=next(item for item in lock["package"] if item["name"]==source["crate"] and item["version"]==source["version"])
        if source["sha256"]!=locked["checksum"]:
            raise RuntimeError("Rebuild source checksum differs from Cargo.lock")
    generation=json.loads((root/"resources/english/generation.json").read_text())
    if generation["generator_sha256"] != english["generator"]["sha256"] or generation["sources_sha256"] != digest(root/"resources/english/sources.json"):
        raise RuntimeError("English generation inputs differ from committed identities")
    return {"source_replay":english["source_replay"],"english_words":generation["words"],"parent_pages":generation["parent_pages"]}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True, help="Lindera build cache directory")
    args = parser.parse_args()
    if "DOCS_RS" in os.environ:
        parser.error("DOCS_RS is prohibited: real embedded resources are required")
    manifest = json.loads((ROOT / "resources/manifest.json").read_text())
    notices = verify_notice_files(ROOT / "resources", manifest.get("notice_files"))
    project_data = verify_project_data(ROOT,manifest)
    archives = Path(os.environ.get("PGCB_RESOURCE_ARCHIVES", Path.home() / ".cache/pg_charabia/resources"))
    archives.mkdir(parents=True, exist_ok=True)
    destination = args.cache / (manifest["lindera"] + "-fmt2")
    destination.mkdir(parents=True, exist_ok=True)
    verified = []
    for item in manifest["archives"]:
        archive = archives / item["name"]
        if not archive.exists():
            temporary = archive.with_name(archive.name + f".download-{os.getpid()}")
            try:
                subprocess.run(["curl", "--fail", "--location", "--retry", "0", item["url"], "--output", str(temporary)], check=True)
                if digest(temporary) != item["sha256"]:
                    raise RuntimeError(f"Downloaded archive SHA-256 mismatch; preserved {temporary}")
                temporary.replace(archive)
            except BaseException:
                # A partial/failed download remains evidence, never a valid cache entry.
                raise
        actual = digest(archive)
        if actual != item["sha256"]:
            raise RuntimeError(f"Cached archive SHA-256 mismatch: {archive}; preserved for diagnosis")
        staged = destination / item["name"]
        if staged.exists() and digest(staged) != actual:
            raise RuntimeError(f"Lindera cache archive mismatch: {staged}; explicit invalidation required")
        if not staged.exists():
            shutil.copyfile(archive, staged)
        verified.append({"name": item["name"], "sha256": actual, "bytes": archive.stat().st_size})
    print(json.dumps({"manifest_sha256": digest(ROOT / "resources/manifest.json"), "lindera_cache": str(args.cache), "archives": verified, "notices": notices, "project_data": project_data}, indent=2))

if __name__ == "__main__":
    main()

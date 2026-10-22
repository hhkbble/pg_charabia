#!/usr/bin/env python3
"""Generate pinned English words and parent-page attribution; never download data."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import platform
import re
from urllib.parse import quote

ASCII_WORD = re.compile(r"[a-zA-Z]+")
RULE = "lang_code=en; word/forms[].form only; Python str.split; ASCII fullmatch; ASCII lowercase; byte-sort unique; LF"


def identity(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"sha256": digest.hexdigest(), "bytes": size}


def check_sources(manifest, input_dir):
    if manifest.get("format") != 1:
        raise ValueError("unsupported source manifest format")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("inputs must be a nonempty list")
    checked = []
    seen = set()
    for row in inputs:
        name = row["file"]
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
            raise ValueError("input must use a plain filename")
        if name in seen:
            raise ValueError("duplicate input: " + name)
        seen.add(name)
        if row.get("kind") not in ("kaikki", "esdb") or row.get("compression") not in ("gzip", "none"):
            raise ValueError("invalid input kind or compression: " + name)
        path = input_dir / name
        actual = identity(path)
        if actual["sha256"] != row["sha256"]:
            raise ValueError("SHA-256 mismatch: " + name)
        if actual["bytes"] != row["bytes"]:
            raise ValueError("byte length mismatch: " + name)
        checked.append((row, path))
    if sum(row["kind"] == "kaikki" for row, _ in checked) != 1:
        raise ValueError("exactly one Kaikki input is required")
    return checked


def text_stream(row, path):
    if row["compression"] == "gzip":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def generate(manifest_path, input_dir, output_dir):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checked = check_sources(manifest, input_dir)
    destinations = {output_dir / "words.txt", output_dir / "generation.json", output_dir / "attribution/headword-pages.txt.gz"}
    protected = [manifest_path, *(path for _, path in checked)]
    if any(path.resolve() == target.resolve() or (target.exists() and path.samefile(target))
           for path in protected for target in destinations):
        raise ValueError("output must not overwrite an input")
    words = set()
    pages = set()
    counts = {"english_records": 0, "word_fields": 0, "form_fields": 0,
              "candidate_parts": 0, "rejected_parts": 0, "esdb_lines": 0}

    def add(raw):
        if not isinstance(raw, str):
            raise ValueError("word/form must be a string")
        # This intentionally includes Python's U+001C..U+001F whitespace rules.
        for part in raw.split():
            counts["candidate_parts"] += 1
            if ASCII_WORD.fullmatch(part):
                words.add(part.lower())
            else:
                counts["rejected_parts"] += 1

    for row, path in checked:
        with text_stream(row, path) as source:
            if row["kind"] == "kaikki":
                for number, line in enumerate(source, 1):
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError(f"Kaikki line {number} must be an object")
                    if entry.get("lang_code") != "en":
                        continue
                    counts["english_records"] += 1
                    word = entry["word"]
                    add(word)
                    counts["word_fields"] += 1
                    # Forms cite the original parent page, never a fabricated lowercased title.
                    pages.add("https://en.wiktionary.org/wiki/" + quote(word.replace(" ", "_"), safe=""))
                    forms = entry.get("forms", [])
                    if not isinstance(forms, list):
                        raise ValueError(f"Kaikki line {number} forms must be a list")
                    for form in forms:
                        if not isinstance(form, dict):
                            raise ValueError(f"Kaikki line {number} form must be an object")
                        add(form["form"])
                        counts["form_fields"] += 1
            else:
                for line in source:
                    add(line)
                    counts["esdb_lines"] += 1

    data = ("\n".join(sorted(words)) + ("\n" if words else "")).encode("ascii")
    word_identity = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "entries": len(words)}
    if "expected_words" in manifest and word_identity != manifest["expected_words"]:
        raise ValueError("generated words differ from the pinned output identity")
    pages_data = ("\n".join(sorted(pages)) + ("\n" if pages else "")).encode("utf-8")
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as destination:
        destination.write(pages_data)
    attribution = compressed.getvalue()
    result = {
        "format": 1, "rule": RULE, "python": platform.python_version(),
        "generator_sha256": identity(Path(__file__))["sha256"],
        "sources_sha256": identity(manifest_path)["sha256"],
        "words": len(words), "parent_pages": len(pages), "counts": counts,
        "outputs": {
            "words.txt": {"sha256": word_identity["sha256"], "bytes": len(data)},
            "attribution/headword-pages.txt.gz": {"sha256": hashlib.sha256(attribution).hexdigest(), "bytes": len(attribution)},
        },
    }
    # All sources, schema and the frozen result have been checked before creating outputs.
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "attribution").mkdir(exist_ok=True)
    (output_dir / "words.txt").write_bytes(data)
    (output_dir / "attribution/headword-pages.txt.gz").write_bytes(attribution)
    (output_dir / "generation.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.manifest, args.input_dir, args.output_dir), indent=2))


if __name__ == "__main__":
    main()

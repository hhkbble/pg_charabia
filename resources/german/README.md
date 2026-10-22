# German dictionary source and rebuilding

The German segmentation data is separate from the extension's MIT code. Its
source declares LGPL-3.0-or-later. Keep these files with every distribution that
embeds the German FST:

- [`german-words-source.txt`](../notices/german-words-source.txt): the complete,
  unmodified corresponding word source from Charabia 0.10.0.
- [`german-decompounder-NOTICE.txt`](../notices/german-decompounder-NOTICE.txt):
  the complete attribution notice, including its separate hyphenation notices.
- [`LGPL-3.0.txt`](../notices/LGPL-3.0.txt) and
  [`GPL-3.0.txt`](../notices/GPL-3.0.txt): the applicable license texts.
- [`rebuild-german`](../../scripts/rebuild-german): this project's actual rebuild
  tool. The pinned Charabia source does not provide the generation tool claimed
  by older notices.

## Reproduce the embedded bytes

Prerequisites are Python 3, Cargo, the repository's Rust toolchain, and the pinned
`fst` 0.4.7 crate in the Cargo cache. The extension's dependency build supplies
that crate. The tool runs Cargo offline in its own temporary directory and
removes the temporary build on exit. It does not alter this repository's lockfile.

```sh
./scripts/rebuild-german --output /path/to/owned-output/german-words.fst
```

The source SHA-256 must be
`9a643bc1e0ce04337100a4b62a39d3e8ead004458d46f5ddf01c0c02ec5844a1`.
The resulting 142,094-byte FST must have SHA-256
`fafef0c1258549ed3a7fac0d73bbcc874226e58d43d61eb20bddf30256ed96e2`.
The tool checks both. Include **all** source lines when generating keys, including
header comments and empty lines. Removing them changes the upstream bytes.

The corresponding source is
[Charabia's pinned word file](https://github.com/meilisearch/charabia/blob/bc63b9860434f06cf78b51bb0eab00aaefb69f15/charabia/dictionaries/txt/german/words.txt).
The exact crate checksum and active resource identity are recorded in the main
resource manifest.

## Modify and relink

For an intentional data edit, copy the source, preserve its license and notices,
and use `--source /path/to/edited-words.txt --allow-modified-source`. The tool
reports the new identity; it does not claim that edited bytes reproduce upstream.

To build an extension with changed data, unpack the exact Charabia 0.10.0 crate
into an owned source directory, replace its
`dictionaries/txt/german/words.txt` and `dictionaries/fst/german/words.fst`, and use
a Cargo `[patch.crates-io]` override in an owned copy of the extension source.
Build and link that source with the supplied lockfile and documented build
prerequisites. This is a recipient's modification procedure, not a project fork
or a change to the shipped dependency. Recompute the resource manifest and run
the analysis and integration checks before using modified postings.

A binary package must provide the complete extension/application source and
build material needed to relink with modified library/data, or an equivalent
license-compliant source arrangement. A license inventory, download URL, or this
recipe alone does not substitute for that material. Retain the exact dependency
source archives and hashes for later recovery. No application key or signature
is needed to load a recipient-rebuilt extension.

These are distribution requirements and a concrete rebuild path. They are not a
claim that every possible redistribution or modification satisfies all licenses.

# English word data

This directory contains data, not MIT-licensed Rust code. The English word set
combines Wiktionary/Kaikki headwords and word forms with nine pinned English
Speller Database (ESDB) lists. No definitions or translations are extracted.

The merged word data and its generated FST are distributed under
[CC BY-SA 4.0](../notices/CC-BY-SA-4.0.txt), with the additional applicable ESDB
notices retained in [ESDB-Copyright.txt](../notices/ESDB-Copyright.txt).

## Attribution and changes

English Wiktionary contributors created the Wiktionary content.
[`attribution/headword-pages.txt.gz`](attribution/headword-pages.txt.gz) lists the
original parent pages in UTF-8, one URL per line. Each link provides access to
the page, its history, and its contributors. Forms cite their original parent
headword; they are not attributed to invented lowercase page titles. Retain this
attribution file, the license, and this notice with the TXT and generated FST.

[Kaikki's English dictionary](https://kaikki.org/dictionary/English/) supplies the
postprocessed export extracted on October 21, 2026, from the October 1, 2026
English Wiktionary dump. Its extraction uses wiktextract revisions `c1bb342` and
`6f96ddf`; the site also reports postprocessing and data merged from other
sources. The downloaded page snapshot is preserved as
[`attribution/kaikki-English.html`](attribution/kaikki-English.html). The
[Wikimedia Terms of Use](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use#7._Licensing_of_Content)
and [Wiktionary copyright page](https://en.wiktionary.org/wiki/Wiktionary:Copyrights)
remain relevant upstream references. This project does not assert that a word
list or FST transformation removes upstream data rights.

Project changes are: keep only `lang_code=en`; read `word` and `forms[].form`;
split with Python `str.split()`; accept complete `[a-zA-Z]+` tokens; lowercase
ASCII; merge the selected ESDB inputs; sort, deduplicate, and write LF endings.
Do not infer the original author's language from membership in this set. Do not
use it as a spelling or language-quality guarantee.

## Required ESDB attribution

**Copyright 2000-2026 by Kevin Atkinson.** The selected inputs include Australian
English lists, with **Copyright 2016 by Benjamin Titze**, and large lists that
include the UK Advanced Cryptics Dictionary, **Copyright (c) J Ross Beresford
1993-1999. All Rights Reserved.** The complete, unabridged permission and
copyright text, including its WordNet section, is in
[`ESDB-Copyright.txt`](../notices/ESDB-Copyright.txt). Keep that text with every
redistribution; do not substitute the shorter non-Australian speller exception.

## Reproduce and retain the data

[`sources.json`](sources.json) pins every original input URL, byte count, and
SHA-256. Large original inputs are retained outside Git; their hashes, not a
moving URL, identify the source. Place them in an owned input directory under
the exact filenames in that manifest, then run:

```sh
./scripts/generate-english.py \
  --manifest resources/english/sources.json \
  --input-dir /path/to/retained-inputs \
  --output-dir /path/to/owned-generated-output
```

The tool verifies all input hashes before creating output. It does not download
or accept a newer source. It also rejects a result that differs from the pinned
TXT identity. [`generation.json`](generation.json) records the generator,
manifest, Python version, counts, and output hashes from the complete replay.
The small independent generator checks run with
`./scripts/check-english-generator`.

The TXT and attribution are source materials. Cargo generates the FST in
`OUT_DIR`; it is not tracked separately. Updating the data requires an explicit
source change, attribution regeneration, quality review, and index maintenance.
Retain the exact installation package, manifest, data and licenses with backups;
WAL and physical database backups do not supply these external installation
resources. See the root README for database maintenance and recovery steps.

These notices record source and redistribution requirements. They are not a
blanket legal approval for other uses or all possible redistribution choices.

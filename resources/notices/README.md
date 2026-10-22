# Embedded resource notices

The extension embeds the resources selected by Charabia 0.10.0. Their exact
identity and archive SHA-256 values are recorded in `resources/manifest.json`.
No resource is downloaded at PostgreSQL runtime.

- Charabia is MIT licensed (`charabia-MIT.txt`).
- UniDic MeCab 2.1.2 offers alternative licenses. This distribution selects its
  BSD alternative, reproduced in `lindera-unidic-NOTICE.txt`. The original
  archive's full COPYING is also preserved in `unidic/COPYING.txt`.
- MeCab KoDic 2.1.1-20180720 uses Apache-2.0. The archive COPYING and Lindera
  notice are preserved in `ko-dic/COPYING.txt` and
  `lindera-ko-dic-NOTICE.txt`.
- German segmentation uses the pinned LGPL-3.0-or-later dictionary. The
  unmodified source is `german-words-source.txt`. The original NOTICE and
  complete LGPL-3.0 and GPL-3.0 texts accompany it. Run `scripts/rebuild-german`
  from the source checkout to reproduce the FST, including all original header
  and empty lines. Read `resources/german/README.md` for the recipient's source,
  modification, rebuild, and relink requirements. This data license is separate
  from the MIT crate license. The delivery must include the corresponding
  complete source and application build materials.
- English lexical data combines the fixed Wiktionary/Kaikki English source and
  ESDB inputs. The derived TXT and FST retain applicable CC BY-SA 4.0 conditions,
  source attribution, and modification notices. ESDB also retains its complete
  applicable Copyright, including AU and large-list requirements. Read
  `resources/english/NOTICE.md` and retain `CC-BY-SA-4.0.txt`,
  `ESDB-Copyright.txt`, and the source/page attribution with the data.
- Charabia identifies PyThaiNLP/nlpo3 as its Thai dictionary source and ICU as
  its Khmer dictionary source. Their upstream license/notice snapshots are
  included; `manifest.json` records the exact snapshot commit and checksum.
  These commits identify notice provenance, not a claim that the bundled
  data was regenerated from those revisions.
- qdrant-rust-stemmers 1.2.2 retains its MIT crate license and the Snowball
  algorithm notice in `qdrant-rust-stemmers-LICENSE` and
  `qdrant-rust-stemmers-algorithms-LICENSE`.
- stop-words 0.10.1 retains its MIT/Apache license files. Its ISO source uses
  the separate `stopwords-iso-LICENSE`. The bundled NLTK attribution README
  and pinned corpus metadata are preserved separately. The metadata does
  not specify licenses for individual source lists; no public-domain claim
  is made. The fixed features choose the NLTK whole table first, with ISO
  covering missing languages.

The Cargo dependency license inventory accompanies release packages. These
resource notices must remain in every binary distribution; a crate metadata
license string does not replace them. `notice_files` in the manifest locks
every distributed local notice, including this explanation, by SHA-256.

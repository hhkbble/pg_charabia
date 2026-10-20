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
- Charabia's German dictionary source explicitly states LGPL-3.0-or-later.
  The corresponding unmodified source is `german-words-source.txt`; the
  upstream attribution notice and both LGPL-3.0 and GPL-3.0 texts accompany
  it. This data license is distinct from Charabia's crate license. Rebuild
  the FST using the pinned Charabia source's dictionary generation tools
  when modifying the data. The extension does not modify this source.
- Charabia identifies PyThaiNLP/nlpo3 as its Thai dictionary source and ICU as
  its Khmer dictionary source. Their upstream license/notice snapshots are
  included; `manifest.json` records the exact snapshot commit and checksum.
  These commits identify notice provenance, not a claim that the bundled
  data was regenerated from those revisions.

The Cargo dependency license inventory accompanies release packages. These
resource notices must remain in every binary distribution; a crate metadata
license string does not replace them.

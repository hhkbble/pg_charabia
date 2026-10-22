//! Immutable embedded resources. Dictionary resets never own these bytes.
use crate::language_data::LanguageId;
use std::sync::LazyLock;

pub(crate) struct StopData {
    pub bytes: &'static [u8],
    pub excluded: usize,
}
include!(concat!(env!("OUT_DIR"), "/word_sets.rs"));

pub(crate) struct StopSet {
    pub fst: fst::Set<&'static [u8]>,
    pub excluded_builtin_count: usize,
}
static STOPS: LazyLock<Vec<Option<StopSet>>> = LazyLock::new(|| {
    STOP_DATA
        .iter()
        .map(|data| {
            data.as_ref().map(|data| StopSet {
                fst: fst::Set::new(data.bytes).expect("build-validated stop FST"),
                excluded_builtin_count: data.excluded,
            })
        })
        .collect()
});
static ENGLISH: LazyLock<fst::Set<&'static [u8]>> =
    LazyLock::new(|| fst::Set::new(ENGLISH_BYTES).expect("build-validated English FST"));
pub(crate) fn stop_set(language: LanguageId) -> Option<&'static StopSet> {
    STOPS[(language.id() - 1) as usize].as_ref()
}
pub(crate) fn english_contains(lowercase_ascii: &str) -> bool {
    ENGLISH.contains(lowercase_ascii)
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    #[test]
    fn generated_identity_matches_committed_source_manifest() {
        let manifest: serde_json::Value =
            serde_json::from_str(include_str!("../resources/manifest.json")).unwrap();
        for route in LanguageId::ALL {
            let data = STOP_DATA[(route.id() - 1) as usize].as_ref();
            let expected = &manifest["canonical_stop_sets"][route.alias()];
            if let Some(data) = data {
                let set = stop_set(route).unwrap();
                assert_eq!(
                    set.fst.len(),
                    expected["entries"].as_u64().unwrap() as usize
                );
                assert_eq!(
                    data.excluded,
                    expected["excluded_builtin_entries"].as_u64().unwrap() as usize
                );
                let mut hash = Sha256::new();
                for key in set.fst.stream().into_strs().unwrap() {
                    hash.update((key.len() as u64).to_le_bytes());
                    hash.update(key.as_bytes());
                }
                assert_eq!(
                    format!("{:x}", hash.finalize()),
                    expected["sha256"].as_str().unwrap()
                );
                assert_eq!(
                    format!("{:x}", Sha256::digest(data.bytes)),
                    manifest["compiled_stop_sets"][route.alias()]["fst_sha256"]
                        .as_str()
                        .unwrap()
                );
            } else {
                assert_eq!(route, LanguageId::UND);
            }
        }
    }
}

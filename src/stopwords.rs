//! Per-dictionary builtin stop set; PostgreSQL dictCtx owns its only cache.
use std::collections::BTreeSet;

use charabia::TokenKind;

use crate::analysis::{AnalysisError, ErrorKind, prepare};
use crate::routes::LanguageId;

pub(crate) struct EffectiveStops {
    pub fst: fst::Set<Vec<u8>>,
    pub excluded_builtin_count: usize,
}

pub(crate) fn build(language: LanguageId) -> Result<EffectiveStops, AnalysisError> {
    let mut keys = BTreeSet::new();
    let mut excluded_builtin_count = 0;
    if let Some(words) = language.stopword_key().and_then(stop_words::lookup) {
        for raw in words {
            let prepared = prepare(raw, language)?;
            if prepared.kind == TokenKind::Word && !prepared.key.is_empty() {
                keys.insert(prepared.key);
            } else {
                excluded_builtin_count += 1;
            }
        }
    }
    let fst = fst::Set::from_iter(keys).map_err(|error| {
        AnalysisError::new(
            ErrorKind::Internal,
            format!("cannot build builtin stopword FST: {error}"),
        )
    })?;
    Ok(EffectiveStops {
        fst,
        excluded_builtin_count,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use charabia::Language;

    fn language(language: Language) -> LanguageId {
        LanguageId::from_language(Some(language)).unwrap()
    }

    #[test]
    fn source_priority_is_nltk_whole_table_not_union() {
        // The locked NLTK English table has 198 entries. ISO alone includes able.
        let english = stop_words::lookup("en").unwrap();
        assert_eq!(english.len(), 198);
        assert!(english.contains(&"the"));
        assert!(english.contains(&"will"));
        assert!(!english.contains(&"able"));
        let set = build(language(Language::Eng)).unwrap();
        assert!(set.fst.contains("the"));
        assert!(!set.fst.contains("able"));
    }

    #[test]
    fn stop_keys_use_language_casing_and_preserve_accents_before_finalization() {
        let french = build(language(Language::Fra)).unwrap();
        assert!(french.fst.contains("à"));
        assert!(!french.fst.contains("À"));
        let turkish = language(Language::Tur);
        assert_eq!(prepare("İÇİN", turkish).unwrap().key, "için");
        assert!(build(turkish).unwrap().fst.contains("için"));
    }

    #[test]
    fn builtin_sets_are_not_mixed_across_languages() {
        let english = build(language(Language::Eng)).unwrap();
        let french = build(language(Language::Fra)).unwrap();
        assert!(english.fst.contains("the"));
        assert!(!french.fst.contains("the"));
        assert_eq!(build(LanguageId::UND).unwrap().fst.len(), 0);
        assert_eq!(build(language(Language::Kat)).unwrap().fst.len(), 0);
    }
}

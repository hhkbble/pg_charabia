//! Per-dictionary effective stop set; PostgreSQL dictCtx owns its only cache.
use std::collections::BTreeSet;

use charabia::TokenKind;

use crate::analysis::{
    AnalysisError, ErrorKind, MAX_STOP_KEY_BYTES, MAX_STOP_WORD_BYTES, MAX_STOP_WORDS, prepare,
};
use crate::routes::Route;

pub(crate) struct EffectiveStops {
    pub fst: fst::Set<Vec<u8>>,
    pub excluded_builtin_count: usize,
}

/// Validate a custom item without tokenizing it or silently trimming it.
pub fn canonical_custom(raw: &str, route: Route) -> Result<String, AnalysisError> {
    if raw.is_empty() || raw.contains('\0') || raw.chars().any(char::is_whitespace) {
        return Err(AnalysisError::new(
            ErrorKind::InvalidInput,
            "custom stopword must be nonempty, contain no NUL, and contain no whitespace",
        ));
    }
    if raw.len() > MAX_STOP_WORD_BYTES {
        return Err(AnalysisError::new(
            ErrorKind::Limit,
            "custom stopword exceeds 2046 bytes",
        ));
    }
    let prepared = prepare(raw, route)?;
    if prepared.kind != TokenKind::Word || prepared.key.is_empty() {
        return Err(AnalysisError::new(
            ErrorKind::InvalidInput,
            "custom stopword must have a nonempty word key",
        ));
    }
    Ok(prepared.key)
}

pub(crate) fn build(
    route: Route,
    builtin_enabled: bool,
    add: &[String],
    keep: &[String],
) -> Result<EffectiveStops, AnalysisError> {
    // Check the decoded resource budget before canonicalization or deduplication.
    let count = add.len().checked_add(keep.len()).ok_or_else(|| {
        AnalysisError::new(ErrorKind::Limit, "custom stopword element count overflow")
    })?;
    if count > MAX_STOP_WORDS {
        return Err(AnalysisError::new(
            ErrorKind::Limit,
            "custom stopwords exceed 65536 elements",
        ));
    }
    for raw in add.iter().chain(keep) {
        if raw.len() > MAX_STOP_WORD_BYTES {
            return Err(AnalysisError::new(
                ErrorKind::Limit,
                "custom stopword exceeds 2046 bytes",
            ));
        }
    }
    let mut payload = 0usize;
    let mut canonical = |raw: &str| -> Result<String, AnalysisError> {
        let key = canonical_custom(raw, route)?;
        payload = payload.checked_add(key.len()).ok_or_else(|| {
            AnalysisError::new(ErrorKind::Limit, "custom stopword key payload overflow")
        })?;
        if payload > MAX_STOP_KEY_BYTES {
            return Err(AnalysisError::new(
                ErrorKind::Limit,
                "custom stopword keys exceed 16777216 bytes",
            ));
        }
        Ok(key)
    };
    let additions = add
        .iter()
        .map(|raw| canonical(raw))
        .collect::<Result<Vec<_>, _>>()?;
    let retained = keep
        .iter()
        .map(|raw| canonical(raw))
        .collect::<Result<Vec<_>, _>>()?;
    let mut keys = BTreeSet::new();
    let mut excluded_builtin_count = 0;
    if builtin_enabled && let Some(words) = route.stopword_key().and_then(stop_words::lookup) {
        for raw in words {
            let prepared = prepare(raw, route)?;
            if prepared.kind == TokenKind::Word && !prepared.key.is_empty() {
                keys.insert(prepared.key);
            } else {
                excluded_builtin_count += 1;
            }
        }
    }
    keys.extend(additions);
    for key in retained {
        keys.remove(&key);
    }
    let fst = fst::Set::from_iter(keys).map_err(|error| {
        AnalysisError::new(
            ErrorKind::Internal,
            format!("cannot build effective stopword FST: {error}"),
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

    fn route(language: Language) -> Route {
        Route::from_language(Some(language)).unwrap()
    }

    #[test]
    fn source_priority_is_nltk_whole_table_not_union() {
        // The locked NLTK English table has 198 entries. ISO alone includes able.
        let english = stop_words::lookup("en").unwrap();
        assert_eq!(english.len(), 198);
        assert!(english.contains(&"the"));
        assert!(english.contains(&"will"));
        assert!(!english.contains(&"able"));
        let set = build(route(Language::Eng), true, &[], &[]).unwrap();
        assert!(set.fst.contains("the"));
        assert!(!set.fst.contains("able"));
    }

    #[test]
    fn canonical_sets_apply_union_then_keep_and_preserve_accents() {
        let r = route(Language::Fra);
        let set = build(
            r,
            false,
            &["À".into(), "a".into(), "THE".into()],
            &["a\u{300}".into()],
        )
        .unwrap();
        assert!(!set.fst.contains("à"));
        assert!(set.fst.contains("a"));
        assert!(set.fst.contains("the"));
        let english = build(
            route(Language::Eng),
            true,
            &["custom".into()],
            &["THE".into()],
        )
        .unwrap();
        assert!(!english.fst.contains("the"));
        assert!(english.fst.contains("custom"));
    }

    #[test]
    fn custom_validation_and_limits_precede_deduplication() {
        let r = route(Language::Eng);
        for invalid in ["", " ", "a b", "a\tb", "\0", ".", "\u{1}"] {
            assert_eq!(
                canonical_custom(invalid, r).unwrap_err().kind,
                ErrorKind::InvalidInput,
                "{invalid:?}"
            );
        }
        assert_eq!(
            canonical_custom(&"x".repeat(MAX_STOP_WORD_BYTES + 1), r)
                .unwrap_err()
                .kind,
            ErrorKind::Limit
        );
        assert_eq!(
            canonical_custom(&"x".repeat(MAX_STOP_WORD_BYTES), r)
                .unwrap()
                .len(),
            MAX_STOP_WORD_BYTES
        );
        let repeated = vec!["a".to_owned(); MAX_STOP_WORDS];
        assert!(build(r, false, &repeated, &[]).unwrap().fst.contains("a"));
        assert_eq!(
            build(r, false, &repeated, &["a".into()])
                .err()
                .unwrap()
                .kind,
            ErrorKind::Limit
        );
        let expanded = vec!["x".repeat(257); MAX_STOP_WORDS];
        assert_eq!(
            build(r, false, &expanded, &[]).err().unwrap().kind,
            ErrorKind::Limit
        );
    }
}

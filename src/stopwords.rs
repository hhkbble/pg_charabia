//! Stopword access uses immutable build-generated sets, never an INIT builder.
pub(crate) use crate::resources::stop_set;
#[cfg(test)]
mod tests {
    use super::*;
    use crate::routes::LanguageId;
    fn set(alias: &str) -> &'static crate::resources::StopSet {
        stop_set(LanguageId::from_alias(alias).unwrap()).unwrap()
    }
    #[test]
    fn fixed_source_priority_and_canonical_keys() {
        assert_eq!(set("eng").fst.len(), 198);
        assert!(set("eng").fst.contains("the"));
        assert!(!set("eng").fst.contains("able"));
        assert!(set("fra").fst.contains("à"));
        assert!(!set("fra").fst.contains("the"));
        assert!(set("tur").fst.contains("için"));
        assert!(stop_set(LanguageId::UND).is_none());
    }
}

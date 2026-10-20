//! Persistent settings and their canonical identity. No backend-global cache.
use crate::{
    analysis::{self, AnalysisError, ErrorKind},
    routes::Route,
};
use charabia::TokenKind;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct Settings {
    pub builtin_stopwords: bool,
    pub stemming: bool,
    pub stopwords_add: BTreeMap<String, Vec<String>>,
    pub stopwords_keep: BTreeMap<String, Vec<String>>,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            builtin_stopwords: true,
            stemming: true,
            stopwords_add: BTreeMap::new(),
            stopwords_keep: BTreeMap::new(),
        }
    }
}
impl Settings {
    pub fn parse(value: Value) -> Result<Self, AnalysisError> {
        let mut settings: Self = serde_json::from_value(value).map_err(|e| {
            AnalysisError::new(
                ErrorKind::InvalidInput,
                format!("invalid analyzer settings: {e}"),
            )
        })?;
        settings.validate()?;
        settings.stopwords_add.retain(|_, v| !v.is_empty());
        settings.stopwords_keep.retain(|_, v| !v.is_empty());
        Ok(settings)
    }
    pub fn canonical_json(&self) -> String {
        serde_json::to_string(self).expect("settings contain only JSON strings and booleans")
    }
    pub fn fingerprint(&self) -> String {
        format!("{:x}", Sha256::digest(self.canonical_json().as_bytes()))
    }
    pub fn analysis_fingerprint(&self) -> String {
        format!(
            "{:x}",
            Sha256::digest(
                format!(
                    "general-1:{}:{}:{}:{}",
                    env!("PG_CHARABIA_LOCK_SHA256"),
                    env!("PG_CHARABIA_RESOURCES_SHA256"),
                    env!("CARGO_PKG_VERSION"),
                    self.fingerprint()
                )
                .as_bytes()
            )
        )
    }
    pub fn words(&self, route: Route, keep: bool) -> &[String] {
        let map = if keep {
            &self.stopwords_keep
        } else {
            &self.stopwords_add
        };
        map.get(route.alias()).map_or(&[], Vec::as_slice)
    }
    pub fn validate(&self) -> Result<(), AnalysisError> {
        if self.canonical_json().len() > analysis::MAX_STOP_WORDS_JSON_BYTES {
            return Err(AnalysisError::new(
                ErrorKind::Limit,
                "settings JSON exceeds 1048576 bytes",
            ));
        }
        let count = self
            .stopwords_add
            .values()
            .chain(self.stopwords_keep.values())
            .try_fold(0usize, |n, w| n.checked_add(w.len()))
            .ok_or_else(|| AnalysisError::new(ErrorKind::Limit, "stopword count overflow"))?;
        if count > 65_536 {
            return Err(AnalysisError::new(
                ErrorKind::Limit,
                "add and keep exceed 65536 raw elements",
            ));
        }
        let mut bytes = 0usize;
        for (code, words) in self.stopwords_add.iter().chain(self.stopwords_keep.iter()) {
            let route = Route::from_alias(code)
                .filter(|r| !r.is_display())
                .ok_or_else(|| {
                    AnalysisError::new(
                        ErrorKind::InvalidInput,
                        format!("unknown language code: {code}"),
                    )
                })?;
            for word in words {
                if word.is_empty() || word.contains('\0') || word.chars().any(char::is_whitespace) {
                    return Err(AnalysisError::new(
                        ErrorKind::InvalidInput,
                        "custom stopword must be a nonempty single item without NUL or whitespace",
                    ));
                }
                if word.len() > 2046 {
                    return Err(AnalysisError::new(
                        ErrorKind::Limit,
                        "custom stopword exceeds 2046 bytes",
                    ));
                }
                let prepared = analysis::prepare(word, route)?;
                if prepared.kind != TokenKind::Word || prepared.key.is_empty() {
                    return Err(AnalysisError::new(
                        ErrorKind::InvalidInput,
                        "custom stopword canonical key is empty or a separator",
                    ));
                }
                bytes = bytes.checked_add(prepared.key.len()).ok_or_else(|| {
                    AnalysisError::new(ErrorKind::Limit, "canonical stopword size overflow")
                })?;
                if bytes > 16 * 1_048_576 {
                    return Err(AnalysisError::new(
                        ErrorKind::Limit,
                        "canonical stopword keys exceed 16777216 bytes",
                    ));
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub struct DictionaryParameters {
    pub route: Route,
    pub stemming: bool,
    pub builtin_stopwords: bool,
    pub add: Vec<String>,
    pub keep: Vec<String>,
    pub settings_fingerprint: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn default_identity() {
        assert_eq!(
            Settings::default().canonical_json(),
            r#"{"builtin_stopwords":true,"stemming":true,"stopwords_add":{},"stopwords_keep":{}}"#
        );
        assert_eq!(
            Settings::parse(serde_json::json!({})).unwrap(),
            Settings::default()
        );
    }
    #[test]
    fn complete_replacement_and_validation() {
        assert!(
            !Settings::parse(serde_json::json!({"stemming":false}))
                .unwrap()
                .stemming
        );
        assert!(Settings::parse(serde_json::json!({"stemming":0})).is_err());
        assert!(Settings::parse(serde_json::json!({"stopwords_add":{"en":["word"]}})).is_err());
        assert!(
            Settings::parse(serde_json::json!({"stopwords_add":{"eng":["two words"]}})).is_err()
        );
        assert!(Settings::parse(serde_json::json!({"stopwords_keep":{"eng":["\u{1}"]}})).is_err());
        assert!(Settings::parse(serde_json::json!({"unknown":true})).is_err());
    }
    #[test]
    fn direct_dictionary_options_enforce_the_local_json_budget() {
        let route = Route::from_alias("eng").unwrap();
        let words = vec!["x".repeat(1024); 2048];
        let result = analysis::Options::new(route, true, false, &words, &[]);
        assert!(
            result.is_err(),
            "direct INIT must not bypass the 1 MiB local JSON budget"
        );
    }
}

//! Language completion for the fixed Charabia/whatlang model.
use charabia::{Language, Script};

use crate::analysis::{AnalysisError, ErrorKind, Span};
use crate::routes::Route;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LanguageSource {
    Charabia,
    Script,
    Supplement,
    None,
}

impl LanguageSource {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Charabia => "charabia",
            Self::Script => "script",
            Self::Supplement => "supplement",
            Self::None => "none",
        }
    }
}

fn singleton(script: Script) -> Option<Language> {
    Some(match script {
        Script::Bengali => Language::Ben,
        Script::Hangul => Language::Kor,
        Script::Georgian => Language::Kat,
        Script::Greek => Language::Ell,
        Script::Kannada => Language::Kan,
        Script::Tamil => Language::Tam,
        Script::Thai => Language::Tha,
        Script::Gujarati => Language::Guj,
        Script::Gurmukhi => Language::Pan,
        Script::Telugu => Language::Tel,
        Script::Malayalam => Language::Mal,
        Script::Oriya => Language::Ori,
        Script::Myanmar => Language::Mya,
        Script::Sinhala => Language::Sin,
        Script::Khmer => Language::Khm,
        Script::Ethiopic => Language::Amh,
        Script::Armenian => Language::Hye,
        _ => return None,
    })
}

fn route_for(script: Script, language: Option<Language>) -> Result<Route, AnalysisError> {
    let route = Route::from_language(language).ok_or_else(|| {
        AnalysisError::new(
            ErrorKind::Internal,
            "language is outside the fixed route table",
        )
    })?;
    if route.script() != script {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "selected language does not belong to the raw token script",
        ));
    }
    Ok(route)
}

/// Complete metadata, returning the number of statistical detector calls.
///
/// The fixed predicate audit proves that contiguous missing4 spans recover their
/// original script group. Armenian overlaps do not need statistical detection.
/// This is a grouping invariant, not a language-quality guarantee.
pub(crate) fn complete(input: &str, spans: &mut [Span]) -> Result<usize, AnalysisError> {
    let mut calls = 0;
    let mut first = 0;
    while first < spans.len() {
        let script = spans[first].script;
        let mut end = first + 1;
        while end < spans.len() && spans[end].script == script {
            end += 1;
        }
        let has_missing = spans[first..end]
            .iter()
            .any(|span| span.charabia_language.is_none());
        let completed = if !has_missing {
            None
        } else if let Some(language) = singleton(script) {
            Some((Some(language), LanguageSource::Script))
        } else if matches!(
            script,
            Script::Latin | Script::Cyrillic | Script::Hebrew | Script::Devanagari
        ) {
            calls += 1;
            let raw = &input[spans[first].start..spans[end - 1].end];
            let language = whatlang::detect_lang(raw)
                .map(Language::from)
                .ok_or_else(|| {
                    AnalysisError::new(
                        ErrorKind::Internal,
                        "fixed model returned no language for a recognized script group",
                    )
                })?;
            Some((Some(language), LanguageSource::Supplement))
        } else if script == Script::Other {
            Some((None, LanguageSource::None))
        } else {
            return Err(AnalysisError::new(
                ErrorKind::Internal,
                "Charabia omitted language on a route that requires existing metadata",
            ));
        };
        for span in &mut spans[first..end] {
            let (language, source) = match span.charabia_language {
                Some(language) => (Some(language), LanguageSource::Charabia),
                None => completed.ok_or_else(|| {
                    AnalysisError::new(ErrorKind::Internal, "missing language completion")
                })?,
            };
            span.route = route_for(script, language)?;
            span.language_source = source;
        }
        first = end;
    }
    Ok(calls)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn singleton_mapping_is_exactly_the_model_unique_scripts() {
        let mut counts = std::collections::HashMap::new();
        for language in whatlang::Lang::all() {
            let route = Route::from_language(Some(Language::from(*language))).unwrap();
            *counts.entry(route.script()).or_insert(0) += 1;
        }
        // Counts come from the locked 70-language model, not detector quality.
        for (script, expected) in [
            (Script::Latin, 37),
            (Script::Cyrillic, 6),
            (Script::Hebrew, 2),
            (Script::Devanagari, 3),
            (Script::Arabic, 3),
            (Script::Cj, 2),
        ] {
            assert_eq!(counts.get(&script), Some(&expected));
        }
        assert!(!counts.contains_key(&Script::Other));
        let singletons: Vec<_> = counts.iter().filter(|(_, count)| **count == 1).collect();
        assert_eq!(singletons.len(), 17);
        for (script, _) in singletons {
            let language = singleton(*script).expect("every unique model script has a map");
            assert_eq!(
                route_for(*script, Some(language)).unwrap().script(),
                *script
            );
        }
        for script in [
            Script::Latin,
            Script::Cyrillic,
            Script::Hebrew,
            Script::Devanagari,
        ] {
            assert!(singleton(script).is_none());
        }
    }

    #[test]
    fn invalid_metadata_is_an_invariant_error_not_a_fallback() {
        assert_eq!(
            route_for(Script::Latin, Some(Language::Rus))
                .unwrap_err()
                .kind,
            ErrorKind::Internal
        );
        assert_eq!(
            route_for(Script::Latin, None).unwrap_err().kind,
            ErrorKind::Internal
        );
        assert_eq!(route_for(Script::Other, None).unwrap(), Route::UND);
        assert!(Route::from_language(Some(Language::Zho)).is_none());
    }
}

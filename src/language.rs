//! Metadata completion for the fixed, closed 15-language candidate set.
use charabia::{Language, Script};

use crate::analysis::{AnalysisError, Span};
use crate::routes::LanguageId;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LanguageSource {
    Charabia,
    Script,
    None,
    EnglishMerge,
}

impl LanguageSource {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Charabia => "charabia",
            Self::Script => "script",
            Self::None => "none",
            Self::EnglishMerge => "english-merge",
        }
    }
}

fn singleton(script: Script) -> Option<Language> {
    match script {
        Script::Cyrillic => Some(Language::Rus),
        Script::Hangul => Some(Language::Kor),
        Script::Thai => Some(Language::Tha),
        _ => None,
    }
}

fn route_for(script: Script, language: Option<Language>) -> LanguageId {
    language
        .and_then(|language| LanguageId::from_language(Some(language)))
        .filter(|route| route.script() == script)
        .unwrap_or(LanguageId::UND)
}

/// Reuse compatible upstream metadata; only three missing paths need a map.
///
/// In the pinned feature map, Latin/Cj/Arabic already request detection while
/// Cyrillic/Hangul/Thai do not. Repeating a failed detection changes neither
/// the evidence nor its meaning. Unknown and out-of-scope inputs remain valid.
pub(crate) fn complete(_input: &str, spans: &mut [Span]) -> Result<usize, AnalysisError> {
    for span in spans {
        match span.charabia_language {
            Some(language) => {
                span.route = route_for(span.script, Some(language));
                span.language_source = if span.route == LanguageId::UND {
                    LanguageSource::None
                } else {
                    LanguageSource::Charabia
                };
            }
            None => {
                span.route = route_for(span.script, singleton(span.script));
                span.language_source = if span.route == LanguageId::UND {
                    LanguageSource::None
                } else {
                    LanguageSource::Script
                };
            }
        }
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::routes::TokenType;

    #[test]
    fn only_three_missing_paths_have_direct_mappings() {
        for (script, expected) in [
            (Script::Latin, None),
            (Script::Cj, None),
            (Script::Arabic, None),
            (Script::Cyrillic, Some(Language::Rus)),
            (Script::Hangul, Some(Language::Kor)),
            (Script::Thai, Some(Language::Tha)),
            (Script::Greek, None),
            (Script::Hebrew, None),
            (Script::Devanagari, None),
            (Script::Bengali, None),
            (Script::Armenian, None),
            (Script::Other, None),
        ] {
            assert_eq!(singleton(script), expected);
        }
        // The finite target set has nine Latin and two Cj languages. These
        // checks describe its routes, not statistical identification accuracy.
        assert_eq!(
            LanguageId::ALL
                .iter()
                .filter(|l| l.script() == Script::Latin)
                .count(),
            9
        );
        assert_eq!(
            LanguageId::ALL
                .iter()
                .filter(|l| l.script() == Script::Cj)
                .count(),
            2
        );
    }

    #[test]
    fn existing_metadata_must_be_in_range_and_script_compatible() {
        assert_eq!(
            route_for(Script::Latin, Some(Language::Eng)),
            LanguageId::ENG
        );
        for (script, language) in [
            (Script::Latin, Some(Language::Rus)),
            (Script::Latin, Some(Language::Swe)),
            (Script::Greek, Some(Language::Ell)),
            (Script::Cj, Some(Language::Zho)),
            (Script::Other, Some(Language::Eng)),
            (Script::Latin, None),
        ] {
            assert_eq!(route_for(script, language), LanguageId::UND);
        }
    }

    #[test]
    fn unknown_metadata_is_preserved_and_never_redetected() {
        let samples = [
            (
                Script::Latin,
                Some(Language::Swe),
                LanguageId::UND,
                LanguageSource::None,
            ),
            (
                Script::Latin,
                Some(Language::Rus),
                LanguageId::UND,
                LanguageSource::None,
            ),
            (Script::Latin, None, LanguageId::UND, LanguageSource::None),
            (
                Script::Cyrillic,
                None,
                LanguageId::from_alias("rus").unwrap(),
                LanguageSource::Script,
            ),
            (
                Script::Cyrillic,
                Some(Language::Ukr),
                LanguageId::UND,
                LanguageSource::None,
            ),
            (
                Script::Thai,
                None,
                LanguageId::from_alias("tha").unwrap(),
                LanguageSource::Script,
            ),
        ];
        let mut spans: Vec<_> = samples
            .iter()
            .map(|(script, language, _, _)| Span {
                start: 0,
                end: 1,
                char_start: 0,
                char_end: 1,
                route: LanguageId::UND,
                token_type: TokenType::Blank,
                script: *script,
                charabia_language: *language,
                language_source: LanguageSource::None,
            })
            .collect();
        assert_eq!(complete("x", &mut spans).unwrap(), 0);
        for (span, (_, language, route, source)) in spans.iter().zip(samples) {
            assert_eq!(span.charabia_language, language);
            assert_eq!(span.route, route);
            assert_eq!(span.language_source, source);
        }
    }
}

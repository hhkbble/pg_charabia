//! Fixed language routes; token IDs are generated from this single declaration.
use charabia::{Language, Script};
use qdrant_rust_stemmers::Algorithm;

pub const LANGUAGE_COUNT: usize = 70;
pub const ROUTE_COUNT: usize = 71;
pub const TYPE_COUNT: usize = 73;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct LanguageId(u8);

const LANGUAGES: [(Language, Script, &str, Option<Algorithm>); LANGUAGE_COUNT] = [
    (Language::Epo, Script::Latin, "eo", None),
    (Language::Eng, Script::Latin, "en", Some(Algorithm::English)),
    (
        Language::Rus,
        Script::Cyrillic,
        "ru",
        Some(Algorithm::Russian),
    ),
    (Language::Cmn, Script::Cj, "zh", None),
    (Language::Spa, Script::Latin, "es", Some(Algorithm::Spanish)),
    (
        Language::Por,
        Script::Latin,
        "pt",
        Some(Algorithm::Portuguese),
    ),
    (Language::Ita, Script::Latin, "it", Some(Algorithm::Italian)),
    (Language::Ben, Script::Bengali, "bn", None),
    (Language::Fra, Script::Latin, "fr", Some(Algorithm::French)),
    (Language::Deu, Script::Latin, "de", Some(Algorithm::German)),
    (Language::Ukr, Script::Cyrillic, "uk", None),
    (Language::Kat, Script::Georgian, "", None),
    (Language::Ara, Script::Arabic, "ar", Some(Algorithm::Arabic)),
    (Language::Hin, Script::Devanagari, "hi", None),
    (Language::Jpn, Script::Cj, "ja", None),
    (Language::Heb, Script::Hebrew, "he", None),
    (Language::Yid, Script::Hebrew, "", None),
    (Language::Pol, Script::Latin, "pl", None),
    (Language::Amh, Script::Ethiopic, "", None),
    (Language::Jav, Script::Latin, "", None),
    (Language::Kor, Script::Hangul, "ko", None),
    (
        Language::Nob,
        Script::Latin,
        "no",
        Some(Algorithm::Norwegian),
    ),
    (Language::Dan, Script::Latin, "da", Some(Algorithm::Danish)),
    (Language::Swe, Script::Latin, "sv", Some(Algorithm::Swedish)),
    (Language::Fin, Script::Latin, "fi", Some(Algorithm::Finnish)),
    (Language::Tur, Script::Latin, "tr", Some(Algorithm::Turkish)),
    (Language::Nld, Script::Latin, "nl", Some(Algorithm::Dutch)),
    (
        Language::Hun,
        Script::Latin,
        "hu",
        Some(Algorithm::Hungarian),
    ),
    (Language::Ces, Script::Latin, "cs", None),
    (Language::Ell, Script::Greek, "el", Some(Algorithm::Greek)),
    (Language::Bul, Script::Cyrillic, "bg", None),
    (Language::Bel, Script::Cyrillic, "be", None),
    (Language::Mar, Script::Devanagari, "mr", None),
    (Language::Kan, Script::Kannada, "", None),
    (
        Language::Ron,
        Script::Latin,
        "ro",
        Some(Algorithm::Romanian),
    ),
    (Language::Slv, Script::Latin, "sl", None),
    (Language::Hrv, Script::Latin, "hr", None),
    (Language::Srp, Script::Cyrillic, "", None),
    (Language::Mkd, Script::Cyrillic, "", None),
    (Language::Lit, Script::Latin, "lt", None),
    (Language::Lav, Script::Latin, "lv", None),
    (Language::Est, Script::Latin, "et", None),
    (Language::Tam, Script::Tamil, "ta", Some(Algorithm::Tamil)),
    (Language::Vie, Script::Latin, "vi", None),
    (Language::Urd, Script::Arabic, "ur", None),
    (Language::Tha, Script::Thai, "th", None),
    (Language::Guj, Script::Gujarati, "gu", None),
    (Language::Uzb, Script::Latin, "uz", None),
    (Language::Pan, Script::Gurmukhi, "", None),
    (Language::Aze, Script::Latin, "az", None),
    (Language::Ind, Script::Latin, "id", None),
    (Language::Tel, Script::Telugu, "", None),
    (Language::Pes, Script::Arabic, "fa", None),
    (Language::Mal, Script::Malayalam, "", None),
    (Language::Ori, Script::Oriya, "", None),
    (Language::Mya, Script::Myanmar, "", None),
    (Language::Nep, Script::Devanagari, "ne", None),
    (Language::Sin, Script::Sinhala, "", None),
    (Language::Khm, Script::Khmer, "", None),
    (Language::Tuk, Script::Latin, "", None),
    (Language::Aka, Script::Latin, "", None),
    (Language::Zul, Script::Latin, "zu", None),
    (Language::Sna, Script::Latin, "", None),
    (Language::Afr, Script::Latin, "af", None),
    (Language::Lat, Script::Latin, "la", None),
    (Language::Slk, Script::Latin, "sk", None),
    (Language::Cat, Script::Latin, "ca", None),
    (Language::Tgl, Script::Latin, "tl", None),
    (
        Language::Hye,
        Script::Armenian,
        "hy",
        Some(Algorithm::Armenian),
    ),
    (Language::Cym, Script::Latin, "", None),
];

impl LanguageId {
    pub const UND: Self = Self(71);
    pub const ALL: [Self; ROUTE_COUNT] = {
        let mut result = [Self(0); ROUTE_COUNT];
        let mut i = 0;
        while i < ROUTE_COUNT {
            result[i] = Self((i + 1) as u8);
            i += 1;
        }
        result
    };
    pub fn id(self) -> i32 {
        i32::from(self.0)
    }
    pub fn from_id(id: i32) -> Option<Self> {
        if (1..=ROUTE_COUNT as i32).contains(&id) {
            Some(Self(id as u8))
        } else {
            None
        }
    }
    pub fn from_alias(alias: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|r| r.alias() == alias)
    }
    pub fn from_language(language: Option<Language>) -> Option<Self> {
        match language {
            None => Some(Self::UND),
            Some(language) => LANGUAGES
                .iter()
                .position(|r| r.0 == language)
                .map(|i| Self((i + 1) as u8)),
        }
    }
    pub fn language(self) -> Option<Language> {
        LANGUAGES.get(usize::from(self.0) - 1).map(|r| r.0)
    }
    pub fn script(self) -> Script {
        LANGUAGES
            .get(usize::from(self.0) - 1)
            .map_or(Script::Other, |r| r.1)
    }
    pub fn stopword_key(self) -> Option<&'static str> {
        LANGUAGES
            .get(usize::from(self.0) - 1)
            .and_then(|r| (!r.2.is_empty()).then_some(r.2))
    }
    pub fn algorithm(self) -> Option<Algorithm> {
        LANGUAGES.get(usize::from(self.0) - 1).and_then(|r| r.3)
    }
    pub fn alias(self) -> &'static str {
        self.language().map_or("und", |l| l.code())
    }
    pub fn has_stopwords(self) -> bool {
        self.stopword_key().and_then(stop_words::lookup).is_some()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenType {
    Language(LanguageId),
    Blank,
    Display,
}
impl TokenType {
    pub fn from_id(id: i32) -> Option<Self> {
        match id {
            72 => Some(Self::Blank),
            73 => Some(Self::Display),
            _ => LanguageId::from_id(id).map(Self::Language),
        }
    }
    pub fn id(self) -> i32 {
        match self {
            Self::Language(l) => l.id(),
            Self::Blank => 72,
            Self::Display => 73,
        }
    }
    pub fn alias(self) -> &'static str {
        match self {
            Self::Language(l) => l.alias(),
            Self::Blank => "blank",
            Self::Display => "display_only",
        }
    }
    pub fn is_language(self) -> bool {
        matches!(self, Self::Language(_))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn finite_routes() {
        assert_eq!(LanguageId::ALL.len(), ROUTE_COUNT);
        for (i, r) in LanguageId::ALL.into_iter().enumerate() {
            assert_eq!(r.id(), (i + 1) as i32);
            assert_eq!(LanguageId::from_alias(r.alias()), Some(r));
        }
        for l in whatlang::Lang::all() {
            assert!(LanguageId::from_language(Some(Language::from(*l))).is_some());
        }
        assert_eq!(LANGUAGES.iter().filter(|r| r.3.is_some()).count(), 19);
        assert_eq!(
            LANGUAGES
                .iter()
                .filter(|r| stop_words::lookup(r.2).is_some())
                .count(),
            52
        );
        assert!(LanguageId::from_id(0).is_none());
        assert!(LanguageId::from_id(72).is_none());
        assert_eq!(TokenType::from_id(72), Some(TokenType::Blank));
        assert_eq!(TokenType::from_id(73), Some(TokenType::Display));
        assert!(TokenType::from_id(74).is_none());
    }
}

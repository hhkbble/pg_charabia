//! Fixed host/target language data. Token IDs follow this declaration.
use charabia::{Language, Script};
pub const LANGUAGE_COUNT: usize = 15;
pub const ROUTE_COUNT: usize = 16;
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct LanguageId(u8);
pub const LANGUAGES: [(Language, Script, &str, Option<&str>); LANGUAGE_COUNT] = [
    (Language::Eng, Script::Latin, "en", Some("English")),
    (Language::Deu, Script::Latin, "de", Some("German")),
    (Language::Nld, Script::Latin, "nl", Some("Dutch")),
    (Language::Rus, Script::Cyrillic, "ru", Some("Russian")),
    (Language::Spa, Script::Latin, "es", Some("Spanish")),
    (Language::Fra, Script::Latin, "fr", Some("French")),
    (Language::Por, Script::Latin, "pt", Some("Portuguese")),
    (Language::Ita, Script::Latin, "it", Some("Italian")),
    (Language::Ara, Script::Arabic, "ar", Some("Arabic")),
    (Language::Tur, Script::Latin, "tr", Some("Turkish")),
    (Language::Cmn, Script::Cj, "zh", None),
    (Language::Jpn, Script::Cj, "ja", None),
    (Language::Kor, Script::Hangul, "ko", None),
    (Language::Vie, Script::Latin, "vi", None),
    (Language::Tha, Script::Thai, "th", None),
];
impl LanguageId {
    pub const UND: Self = Self(16);
    pub const ENG: Self = Self(1);
    pub const ALL: [Self; ROUTE_COUNT] = {
        let mut out = [Self(0); ROUTE_COUNT];
        let mut i = 0;
        while i < ROUTE_COUNT {
            out[i] = Self((i + 1) as u8);
            i += 1;
        }
        out
    };
    pub fn id(self) -> i32 {
        i32::from(self.0)
    }
    pub fn from_id(id: i32) -> Option<Self> {
        (1..=ROUTE_COUNT as i32)
            .contains(&id)
            .then_some(Self(id as u8))
    }
    pub fn from_alias(alias: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|r| r.alias() == alias)
    }
    pub fn from_language(language: Option<Language>) -> Option<Self> {
        match language {
            None => Some(Self::UND),
            Some(l) => LANGUAGES
                .iter()
                .position(|r| r.0 == l)
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
    // The host generator uses this field; runtime binds generated FST views.
    #[allow(dead_code)]
    pub fn stopword_key(self) -> Option<&'static str> {
        LANGUAGES.get(usize::from(self.0) - 1).map(|r| r.2)
    }
    pub fn stemmer_name(self) -> Option<&'static str> {
        LANGUAGES.get(usize::from(self.0) - 1).and_then(|r| r.3)
    }
    pub fn alias(self) -> &'static str {
        self.language().map_or("und", |l| l.code())
    }
    pub fn has_stopwords(self) -> bool {
        self != Self::UND
    }
    pub fn english_eligible(self) -> bool {
        self.script() == Script::Latin && self != Self::ENG
    }
    pub fn default_english(self, filter: bool) -> bool {
        self.english_eligible() && !filter
    }
    pub fn default_stem(self, english: bool) -> bool {
        self.stemmer_name().is_some() || english
    }
}

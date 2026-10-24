//! Runtime algorithm bindings and PostgreSQL token types.
pub use crate::language_data::LanguageId;
use crate::structured_tokens::StructuredTokenKind;
pub const LINGUISTIC_TYPE_COUNT: usize = 18;
pub const TYPE_COUNT: usize = 23;
use qdrant_rust_stemmers::Algorithm;
impl LanguageId {
    pub fn algorithm(self) -> Option<Algorithm> {
        self.stemmer_name().map(|name| match name {
            "English" => Algorithm::English,
            "German" => Algorithm::German,
            "Dutch" => Algorithm::Dutch,
            "Russian" => Algorithm::Russian,
            "Spanish" => Algorithm::Spanish,
            "French" => Algorithm::French,
            "Portuguese" => Algorithm::Portuguese,
            "Italian" => Algorithm::Italian,
            "Arabic" => Algorithm::Arabic,
            "Turkish" => Algorithm::Turkish,
            _ => unreachable!("fixed algorithm"),
        })
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenType {
    Language(LanguageId),
    Structured(StructuredTokenKind),
    Blank,
    Display,
}
impl TokenType {
    pub fn from_id(id: i32) -> Option<Self> {
        match id {
            17 => Some(Self::Blank),
            18 => Some(Self::Display),
            _ => LanguageId::from_id(id)
                .map(Self::Language)
                .or_else(|| StructuredTokenKind::from_id(id).map(Self::Structured)),
        }
    }
    pub fn id(self) -> i32 {
        match self {
            Self::Language(l) => l.id(),
            Self::Structured(kind) => kind.id(),
            Self::Blank => 17,
            Self::Display => 18,
        }
    }
    pub fn alias(self) -> &'static str {
        match self {
            Self::Language(l) => l.alias(),
            Self::Structured(kind) => kind.alias(),
            Self::Blank => "blank",
            Self::Display => "display_only",
        }
    }
    pub fn is_language(self) -> bool {
        matches!(self, Self::Language(_))
    }
    pub fn is_content(self) -> bool {
        matches!(self, Self::Language(_) | Self::Structured(_))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn finite_routes() {
        assert_eq!(
            LanguageId::ALL.map(|r| r.alias()),
            [
                "eng", "deu", "nld", "rus", "spa", "fra", "por", "ita", "ara", "tur", "cmn", "jpn",
                "kor", "vie", "tha", "und"
            ]
        );
        for (i, r) in LanguageId::ALL.into_iter().enumerate() {
            assert_eq!(r.id(), i as i32 + 1);
            assert_eq!(LanguageId::from_alias(r.alias()), Some(r));
        }
        assert_eq!(
            LanguageId::ALL
                .iter()
                .filter(|r| r.algorithm().is_some())
                .count(),
            10
        );
        assert_eq!(
            LanguageId::ALL
                .iter()
                .filter(|r| r.english_eligible())
                .count(),
            8
        );
        assert_eq!(TokenType::from_id(17), Some(TokenType::Blank));
        assert_eq!(TokenType::from_id(18), Some(TokenType::Display));
        for kind in StructuredTokenKind::ALL {
            assert_eq!(
                TokenType::from_id(kind.id()),
                Some(TokenType::Structured(kind))
            );
        }
        assert_eq!(TokenType::from_id(24), None);
    }
}

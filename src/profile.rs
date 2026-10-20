use charabia::{Language, Script};

/// Stable PostgreSQL token type IDs. Published values must never be reordered.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(i32)]
pub enum NormalizationRoute {
    Basic = 1,
    Case = 2,
    Latin = 3,
    LatinSwe = 4,
    Greek = 5,
    Marks = 6,
    Arabic = 7,
    Persian = 8,
    CjChinese = 9,
    CjJapanese = 10,
    CjUnknown = 11,
    CjOther = 12,
    BasicSwe = 13,
    CaseSwe = 14,
    GreekSwe = 15,
    MarksSwe = 16,
    ArabicSwe = 17,
    CjOtherSwe = 18,
}

impl NormalizationRoute {
    pub const ALL: [Self; 18] = [
        Self::Basic,
        Self::Case,
        Self::Latin,
        Self::LatinSwe,
        Self::Greek,
        Self::Marks,
        Self::Arabic,
        Self::Persian,
        Self::CjChinese,
        Self::CjJapanese,
        Self::CjUnknown,
        Self::CjOther,
        Self::BasicSwe,
        Self::CaseSwe,
        Self::GreekSwe,
        Self::MarksSwe,
        Self::ArabicSwe,
        Self::CjOtherSwe,
    ];

    pub const fn id(self) -> i32 {
        self as i32
    }

    pub const fn alias(self) -> &'static str {
        match self {
            Self::Basic => "basic",
            Self::Case => "case",
            Self::Latin => "latin",
            Self::LatinSwe => "latin_swe",
            Self::Greek => "greek",
            Self::Marks => "marks",
            Self::Arabic => "arabic",
            Self::Persian => "persian",
            Self::CjChinese => "cj_chinese",
            Self::CjJapanese => "cj_japanese",
            Self::CjUnknown => "cj_unknown",
            Self::CjOther => "cj_other",
            Self::BasicSwe => "basic_swe",
            Self::CaseSwe => "case_swe",
            Self::GreekSwe => "greek_swe",
            Self::MarksSwe => "marks_swe",
            Self::ArabicSwe => "arabic_swe",
            Self::CjOtherSwe => "cj_other_swe",
        }
    }

    pub fn from_alias(alias: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|route| route.alias() == alias)
    }

    #[cfg(test)]
    pub fn from_id(id: i32) -> Option<Self> {
        Self::ALL.into_iter().find(|route| route.id() == id)
    }

    pub const fn metadata(self) -> (Script, Option<Language>) {
        match self {
            Self::Basic => (Script::Other, None),
            Self::Case => (Script::Cyrillic, None),
            Self::Latin => (Script::Latin, None),
            Self::LatinSwe => (Script::Latin, Some(Language::Swe)),
            Self::Greek => (Script::Greek, None),
            Self::Marks => (Script::Hebrew, None),
            Self::Arabic => (Script::Arabic, None),
            Self::Persian => (Script::Arabic, Some(Language::Pes)),
            Self::CjChinese => (Script::Cj, Some(Language::Cmn)),
            Self::CjJapanese => (Script::Cj, Some(Language::Jpn)),
            Self::CjUnknown => (Script::Cj, None),
            Self::CjOther => (Script::Cj, Some(Language::Eng)),
            Self::BasicSwe => (Script::Other, Some(Language::Swe)),
            Self::CaseSwe => (Script::Cyrillic, Some(Language::Swe)),
            Self::GreekSwe => (Script::Greek, Some(Language::Swe)),
            Self::MarksSwe => (Script::Hebrew, Some(Language::Swe)),
            Self::ArabicSwe => (Script::Arabic, Some(Language::Swe)),
            Self::CjOtherSwe => (Script::Cj, Some(Language::Swe)),
        }
    }

    pub fn from_metadata(script: Script, language: Option<Language>) -> Self {
        // Swedish recomposition checks language alone, including non-Latin scripts.
        if language == Some(Language::Swe) {
            return match script {
                Script::Latin => Self::LatinSwe,
                Script::Cyrillic | Script::Georgian | Script::Armenian => Self::CaseSwe,
                Script::Greek => Self::GreekSwe,
                Script::Hebrew | Script::Thai => Self::MarksSwe,
                Script::Arabic => Self::ArabicSwe,
                Script::Cj => Self::CjOtherSwe,
                _ => Self::BasicSwe,
            };
        }
        match (script, language) {
            (Script::Latin, _) => Self::Latin,
            (Script::Cyrillic | Script::Georgian | Script::Armenian, _) => Self::Case,
            (Script::Greek, _) => Self::Greek,
            (Script::Hebrew | Script::Thai, _) => Self::Marks,
            (Script::Arabic, Some(Language::Pes)) => Self::Persian,
            (Script::Arabic, _) => Self::Arabic,
            (Script::Cj, Some(Language::Cmn | Language::Zho)) => Self::CjChinese,
            (Script::Cj, Some(Language::Jpn)) => Self::CjJapanese,
            (Script::Cj, None) => Self::CjUnknown,
            (Script::Cj, _) => Self::CjOther,
            _ => Self::Basic,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn published_routes_are_stable_and_round_trip() {
        let aliases = [
            "basic",
            "case",
            "latin",
            "latin_swe",
            "greek",
            "marks",
            "arabic",
            "persian",
            "cj_chinese",
            "cj_japanese",
            "cj_unknown",
            "cj_other",
            "basic_swe",
            "case_swe",
            "greek_swe",
            "marks_swe",
            "arabic_swe",
            "cj_other_swe",
        ];
        for (index, route) in NormalizationRoute::ALL.into_iter().enumerate() {
            assert_eq!(route.id(), index as i32 + 1);
            assert_eq!(route.alias(), aliases[index]);
            assert_eq!(NormalizationRoute::from_id(route.id()), Some(route));
            assert_eq!(NormalizationRoute::from_alias(route.alias()), Some(route));
            let (script, language) = route.metadata();
            assert_eq!(NormalizationRoute::from_metadata(script, language), route);
        }
        for id in [i32::MIN, -1, 0, 19, i32::MAX] {
            assert_eq!(NormalizationRoute::from_id(id), None);
        }
        assert_eq!(NormalizationRoute::from_alias("LATIN"), None);
        assert_eq!(NormalizationRoute::from_alias(" latin"), None);
    }
}

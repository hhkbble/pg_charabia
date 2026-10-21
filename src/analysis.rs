//! One-pass raw segmentation and independent, bounded dictionary analysis.
use std::borrow::Cow;
use std::fmt;

use charabia::normalizer::{
    Classifier, ClassifierOption, CompatibilityDecompositionNormalizer, ControlCharNormalizer,
    Normalizer, NormalizerOption,
};
use charabia::{Normalize, Script, Token, TokenKind, TokenizerBuilder};
use qdrant_rust_stemmers::Stemmer;
use unicode_normalization::UnicodeNormalization;

pub use crate::language::LanguageSource;
use crate::routes::{LanguageId, TokenType};

pub const MAX_INPUT_BYTES: usize = 262_144;
pub const MAX_RAW_TOKEN_BYTES: usize = 2_046;
pub const MAX_STEM_BYTES: usize = 1_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    InvalidInput,
    Limit,
    Internal,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnalysisError {
    pub kind: ErrorKind,
    pub message: String,
}

impl AnalysisError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }
}

impl fmt::Display for AnalysisError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for AnalysisError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Span {
    pub start: usize,
    pub end: usize,
    pub char_start: usize,
    pub char_end: usize,
    pub route: LanguageId,
    pub token_type: TokenType,
    pub script: Script,
    pub charabia_language: Option<charabia::Language>,
    pub language_source: LanguageSource,
}

pub struct Options {
    language: LanguageId,
    discard: bool,
    stem: bool,
    normalize: bool,
    filter: bool,
    stops: Option<crate::stopwords::EffectiveStops>,
    stemmer: Option<Stemmer>,
}

impl fmt::Debug for Options {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Options")
            .field("language", &self.language)
            .field("discard", &self.discard)
            .field("stem", &self.stem)
            .field("normalize", &self.normalize)
            .field("filter", &self.filter)
            .field("fst_bytes", &self.fst_bytes())
            .field("effective_stopwords", &self.effective_stopwords())
            .field("excluded_builtin_count", &self.excluded_builtin_count())
            .finish()
    }
}

impl Options {
    pub fn new(
        language: LanguageId,
        discard: bool,
        stem: bool,
        normalize: bool,
        filter: bool,
    ) -> Result<Self, AnalysisError> {
        if discard && !language.has_stopwords() {
            return Err(AnalysisError::new(
                ErrorKind::InvalidInput,
                "language has no builtin stopword table",
            ));
        }
        if stem && language.algorithm().is_none() {
            return Err(AnalysisError::new(
                ErrorKind::InvalidInput,
                "language has no stemming algorithm",
            ));
        }
        if filter && stem {
            return Err(AnalysisError::new(
                ErrorKind::InvalidInput,
                "Filter requires Stem=false",
            ));
        }
        Ok(Self {
            language,
            discard,
            stem,
            normalize,
            filter,
            stops: discard
                .then(|| crate::stopwords::build(language))
                .transpose()?,
            stemmer: stem
                .then(|| language.algorithm().map(Stemmer::create))
                .flatten(),
        })
    }

    pub fn language(&self) -> LanguageId {
        self.language
    }
    pub fn fst_bytes(&self) -> usize {
        self.stops
            .as_ref()
            .map_or(0, |s| s.fst.as_fst().as_bytes().len())
    }
    pub fn effective_stopwords(&self) -> usize {
        self.stops.as_ref().map_or(0, |s| s.fst.len())
    }
    pub fn excluded_builtin_count(&self) -> usize {
        self.stops.as_ref().map_or(0, |s| s.excluded_builtin_count)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Prepared {
    pub kind: TokenKind,
    pub key: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LexizeOutcome {
    Terms {
        first: String,
        second: Option<String>,
    },
    Filter(String),
    Stop,
    Ignore,
}

impl LexizeOutcome {
    #[cfg(test)]
    pub fn into_terms(self) -> Vec<String> {
        match self {
            Self::Terms { first, second } => {
                let mut terms = vec![first];
                if let Some(second) = second {
                    terms.push(second);
                }
                terms
            }
            Self::Filter(term) => vec![term],
            Self::Stop | Self::Ignore => Vec::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenAnalysis {
    pub prepared: Option<String>,
    pub kind: TokenKind,
    pub outcome: LexizeOutcome,
}

pub fn check_input(input: &str) -> Result<(), AnalysisError> {
    if input.len() > MAX_INPUT_BYTES {
        return Err(AnalysisError::new(
            ErrorKind::Limit,
            "Charabia input exceeds 262144 bytes",
        ));
    }
    if input.contains('\0') {
        return Err(AnalysisError::new(
            ErrorKind::InvalidInput,
            "Charabia input contains NUL",
        ));
    }
    Ok(())
}

pub fn segment(input: &str) -> Result<Vec<Span>, AnalysisError> {
    segment_counted(input).map(|(spans, _)| spans)
}

pub(crate) fn segment_counted(input: &str) -> Result<(Vec<Span>, usize), AnalysisError> {
    check_input(input)?;
    let mut builder = TokenizerBuilder::default();
    let tokenizer = builder.build();
    let mut spans = Vec::new();
    let mut previous_byte = 0;
    let mut previous_char = 0;
    for token in tokenizer.segment(input) {
        let raw = checked_span(input, token.byte_start, token.byte_end, previous_byte)?;
        if raw != token.lemma() {
            return Err(AnalysisError::new(
                ErrorKind::Internal,
                "Charabia segmentation changed the raw text",
            ));
        }
        let char_end = previous_char + raw.chars().count();
        if token.char_start != previous_char || token.char_end != char_end {
            return Err(AnalysisError::new(
                ErrorKind::Internal,
                "Charabia returned an invalid character range",
            ));
        }
        spans.push(Span {
            start: token.byte_start,
            end: token.byte_end,
            char_start: previous_char,
            char_end,
            route: LanguageId::UND,
            token_type: TokenType::Blank,
            script: token.script,
            charabia_language: token.language,
            language_source: LanguageSource::None,
        });
        previous_byte = token.byte_end;
        previous_char = char_end;
    }
    if previous_byte != input.len() {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "Charabia spans do not cover the input",
        ));
    }
    let calls = crate::language::complete(input, &mut spans)?;
    let mut bounded = Vec::with_capacity(spans.len());
    for mut span in spans {
        if span.end - span.start <= MAX_RAW_TOKEN_BYTES {
            span.token_type = if is_word(&input[span.start..span.end]) {
                TokenType::Language(span.route)
            } else {
                TokenType::Blank
            };
            bounded.push(span);
            continue;
        }
        // PG discards oversized raw tokens before HEADLINE sees them. Display
        // fragments preserve every byte without turning a long word into terms.
        let mut first_byte = span.start;
        let mut first_char = span.char_start;
        while first_byte < span.end {
            let mut end = (first_byte + MAX_RAW_TOKEN_BYTES).min(span.end);
            while !input.is_char_boundary(end) {
                end -= 1;
            }
            let char_end = first_char + input[first_byte..end].chars().count();
            bounded.push(Span {
                start: first_byte,
                end,
                char_start: first_char,
                char_end,
                token_type: TokenType::Display,
                ..span
            });
            first_byte = end;
            first_char = char_end;
        }
    }
    Ok((bounded, calls))
}

fn checked_span(
    input: &str,
    start: usize,
    end: usize,
    previous: usize,
) -> Result<&str, AnalysisError> {
    if start != previous || start >= end {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "Charabia returned a noncontiguous or empty span",
        ));
    }
    input.get(start..end).ok_or_else(|| {
        AnalysisError::new(
            ErrorKind::Internal,
            "Charabia returned an invalid UTF-8 byte range",
        )
    })
}

fn normalizer(lossy: bool) -> NormalizerOption<'static> {
    NormalizerOption {
        create_char_map: false,
        lossy,
        classifier: ClassifierOption {
            stop_words: None,
            separators: None,
        },
    }
}

pub fn prepare(raw: &str, route: LanguageId) -> Result<Prepared, AnalysisError> {
    check_input(raw)?;
    let token = Token {
        lemma: Cow::Borrowed(raw),
        script: route.script(),
        language: route.language(),
        ..Token::default()
    }
    .normalize(&normalizer(false));
    if matches!(token.kind, TokenKind::Unknown | TokenKind::StopWord) {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "unexpected Charabia classification without a stop set",
        ));
    }
    let nfc: String = token.lemma.nfc().collect();
    let lowered = if route.language() == Some(charabia::Language::Tur) {
        let mut text = String::with_capacity(nfc.len());
        for character in nfc.chars() {
            match character {
                'I' => text.push('ı'),
                'İ' => text.push('i'),
                other => text.extend(other.to_lowercase()),
            }
        }
        text
    } else {
        nfc.to_lowercase()
    };
    Ok(Prepared {
        kind: token.kind,
        key: lowered.nfc().collect(),
    })
}

fn finalize(key: &str, route: LanguageId) -> String {
    Token {
        kind: TokenKind::Word,
        lemma: Cow::Borrowed(key),
        script: route.script(),
        language: route.language(),
        ..Token::default()
    }
    .normalize(&normalizer(true))
    .lemma
    .into_owned()
}

/// The Parser needs classification only, and must not prepare a dictionary key.
/// This prefix is language independent for the locked feature set.
pub fn is_word(raw: &str) -> bool {
    let mut token = Token {
        lemma: Cow::Borrowed(raw),
        ..Token::default()
    };
    let options = normalizer(false);
    for step in [
        &CompatibilityDecompositionNormalizer as &dyn Normalizer,
        &ControlCharNormalizer,
        &Classifier,
    ] {
        if step.should_normalize(&token) {
            token = step.normalize(token, &options);
        }
    }
    token.kind == TokenKind::Word && !token.lemma.is_empty()
}

pub fn stem_eligible(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= MAX_STEM_BYTES
        && key
            .chars()
            .all(|c| c.is_alphabetic() || unicode_normalization::char::is_combining_mark(c))
}

pub fn inspect(
    raw: &str,
    language: LanguageId,
    options: &Options,
) -> Result<TokenAnalysis, AnalysisError> {
    if language != options.language {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "dictionary options belong to another language",
        ));
    }
    // This independent prepare is necessary: direct LEXIZE and earlier filters
    // can supply bytes which never passed through this Parser.
    let Prepared { kind, key } = prepare(raw, language)?;
    if kind != TokenKind::Word || key.is_empty() {
        return Ok(TokenAnalysis {
            prepared: (!key.is_empty()).then_some(key),
            kind,
            outcome: LexizeOutcome::Ignore,
        });
    }
    if options
        .stops
        .as_ref()
        .is_some_and(|stops| stops.fst.contains(&key))
    {
        return Ok(TokenAnalysis {
            prepared: Some(key),
            kind: TokenKind::StopWord,
            outcome: LexizeOutcome::Stop,
        });
    }
    let finish = |key: &str| {
        if options.normalize {
            finalize(key, language)
        } else {
            key.nfc().collect()
        }
    };
    // prepare already guarantees NFC; only the independently returned stem
    // needs NFC work when the finalizer is disabled.
    let surface = if options.normalize {
        finalize(&key, language)
    } else {
        key.clone()
    };
    let outcome = if options.filter {
        if surface.is_empty() {
            LexizeOutcome::Ignore
        } else {
            check_input(&surface)?;
            LexizeOutcome::Filter(surface)
        }
    } else {
        let stem = options
            .stemmer
            .as_ref()
            .filter(|_| stem_eligible(&key))
            .map(|stemmer| finish(&stemmer.stem(&key)));
        let stem = stem.filter(|term| !term.is_empty() && *term != surface);
        if surface.is_empty() {
            stem.map_or(LexizeOutcome::Ignore, |first| LexizeOutcome::Terms {
                first,
                second: None,
            })
        } else {
            LexizeOutcome::Terms {
                first: surface,
                second: stem,
            }
        }
    };
    Ok(TokenAnalysis {
        prepared: Some(key),
        kind,
        outcome,
    })
}

pub fn normalize(
    raw: &str,
    language: LanguageId,
    options: &Options,
) -> Result<LexizeOutcome, AnalysisError> {
    inspect(raw, language, options).map(|token| token.outcome)
}

#[cfg(test)]
mod tests {
    use super::*;
    use charabia::Language;

    fn route(language: Language) -> LanguageId {
        LanguageId::from_language(Some(language)).unwrap()
    }
    fn terms(raw: &str, language: Language, stemming: bool) -> Vec<String> {
        let route = route(language);
        normalize(
            raw,
            route,
            &Options::new(route, false, stemming, true, false).unwrap(),
        )
        .unwrap()
        .into_terms()
    }

    #[test]
    fn fixed_language_capabilities_bound_the_boolean_state_space() {
        // This verifies the finite option contract, not statistical language quality.
        let mut groups = [0usize; 3];
        let mut legal = 0;
        for language in LanguageId::ALL {
            let stops = language.has_stopwords();
            let stems = language.algorithm().is_some();
            assert!(
                !stems || stops,
                "the fixed resources have no stem-only language"
            );
            groups[if stems {
                0
            } else if stops {
                1
            } else {
                2
            }] += 1;
            for bits in 0..16 {
                let (d, s, n, f) = (bits & 1 != 0, bits & 2 != 0, bits & 4 != 0, bits & 8 != 0);
                let expected = (!d || stops) && (!s || stems) && !(s && f);
                let actual = Options::new(language, d, s, n, f);
                assert_eq!(actual.is_ok(), expected, "{}: {bits}", language.alias());
                legal += usize::from(expected);
                if let Ok(options) = actual {
                    assert_eq!(options.stops.is_some(), d);
                }
            }
        }
        assert_eq!(groups, [19, 33, 19]);
        assert_eq!(legal, 568);
    }

    #[test]
    fn independent_algorithm_goldens_preserve_pre_stem_unicode() {
        // English is the crate's documented example; the accented examples are
        // preserved real upstream API runs in general-language/probes records.
        assert_eq!(
            terms("fruitlessly", Language::Eng, true),
            ["fruitlessly", "fruitless"]
        );
        assert_eq!(terms("aimées", Language::Fra, true), ["aimees", "aim"]);
        assert_eq!(terms("IŞIKLAR", Language::Tur, true), ["isiklar", "isik"]);
        assert_eq!(terms("hästarna", Language::Swe, true), ["hastarna", "hast"]);
        assert_eq!(
            prepare("AIME\u{301}ES", route(Language::Fra)).unwrap().key,
            "aimées"
        );
        assert_eq!(prepare("I\u{307}", route(Language::Tur)).unwrap().key, "i");
        assert_eq!(prepare("I", route(Language::Tur)).unwrap().key, "ı");
        assert_eq!(prepare("I", route(Language::Eng)).unwrap().key, "i");
    }

    #[test]
    fn independent_switches_preserve_surface_and_stop_order() {
        let r = route(Language::Eng);
        let on = Options::new(r, true, true, true, false).unwrap();
        let no_stem = Options::new(r, true, false, true, false).unwrap();
        let no_stop = Options::new(r, false, true, true, false).unwrap();
        let no_fold = Options::new(r, true, true, false, false).unwrap();
        for options in [&on, &no_stem, &no_fold] {
            assert_eq!(normalize("THE", r, options).unwrap(), LexizeOutcome::Stop);
        }
        assert_eq!(
            normalize("running", r, &no_stem).unwrap().into_terms(),
            ["running"]
        );
        assert_eq!(normalize("the", r, &no_stop).unwrap().into_terms(), ["the"]);
        assert_eq!(
            normalize("running", r, &on).unwrap().into_terms(),
            ["running", "run"]
        );
        assert_eq!(no_stop.fst_bytes(), 0);
        assert_eq!(no_stop.effective_stopwords(), 0);
        assert!(on.fst_bytes() > 0);
        assert_eq!(terms("run", Language::Eng, true), ["run"]);
        assert_eq!(terms("123", Language::Eng, true), ["123"]);
        assert_eq!(terms("foo_bar", Language::Eng, true), ["foo_bar"]);
    }

    #[test]
    fn normalize_false_preserves_prepared_accents_and_nfc_stems() {
        for (language, raw, expected) in [
            (Language::Fra, "aimées", ["aimées", "aim"]),
            (Language::Tur, "IŞIKLAR", ["ışıklar", "ışık"]),
            (Language::Swe, "hästarna", ["hästarna", "häst"]),
        ] {
            let r = route(language);
            let options = Options::new(r, false, true, false, false).unwrap();
            assert_eq!(normalize(raw, r, &options).unwrap().into_terms(), expected);
        }
        for (language, raw, plain, folded) in [
            (LanguageId::UND, "CAFÉ", "café", "cafe\u{301}"),
            (route(Language::Jpn), "ダメ", "ダメ", "た\u{3099}め"),
        ] {
            for (fold, expected) in [(false, plain), (true, folded)] {
                let options = Options::new(language, false, false, fold, false).unwrap();
                assert_eq!(
                    normalize(raw, language, &options).unwrap().into_terms(),
                    [expected]
                );
            }
        }
    }

    #[test]
    fn filter_replaces_one_term_and_stop_remains_terminal() {
        let r = route(Language::Eng);
        assert!(Options::new(r, false, true, true, true).is_err());
        let folded = Options::new(r, true, false, true, true).unwrap();
        let prepared = Options::new(r, false, false, false, true).unwrap();
        assert_eq!(
            normalize("CAFÉ", r, &folded).unwrap(),
            LexizeOutcome::Filter("cafe".into())
        );
        assert_eq!(
            normalize("CAFÉ", r, &prepared).unwrap(),
            LexizeOutcome::Filter("café".into())
        );
        assert_eq!(normalize("the", r, &folded).unwrap(), LexizeOutcome::Stop);
        assert_eq!(normalize(" ", r, &folded).unwrap(), LexizeOutcome::Ignore);
        let unsupported = route(Language::Kat);
        assert!(Options::new(unsupported, true, false, true, false).is_err());
        assert!(Options::new(unsupported, false, true, true, false).is_err());
        assert!(Options::new(unsupported, false, false, true, true).is_ok());
    }

    #[test]
    fn actual_prepared_stem_input_is_bounded() {
        let r = route(Language::Eng);
        let options = Options::new(r, false, true, false, false).unwrap();
        let at_limit = format!("{}ies", "a".repeat(MAX_STEM_BYTES - 3));
        let beyond_limit = format!("{}ies", "a".repeat(MAX_STEM_BYTES - 2));
        assert_eq!(
            normalize(&at_limit, r, &options)
                .unwrap()
                .into_terms()
                .len(),
            2
        );
        assert_eq!(
            normalize(&beyond_limit, r, &options).unwrap().into_terms(),
            [beyond_limit]
        );
        // NFKD expands this compatibility ligature before the budget is tested.
        let expanded = "ﬃ".repeat(334);
        let prepared = prepare(&expanded, r).unwrap();
        assert_eq!(prepared.key.len(), 1002);
        assert!(!stem_eligible(&prepared.key));
        for invalid in ["", "123", "word_part", "two words"] {
            assert!(!stem_eligible(invalid));
        }
    }

    #[test]
    fn empty_keys_and_separators_are_ignore_before_stop() {
        let r = route(Language::Eng);
        let options = Options::new(r, true, true, true, false).unwrap();
        for raw in ["", " ", ".", "\u{1}", "\u{301}"] {
            assert_eq!(
                normalize(raw, r, &options).unwrap(),
                LexizeOutcome::Ignore,
                "{raw:?}"
            );
        }
        assert_eq!(
            inspect(" ", r, &options).unwrap().prepared.as_deref(),
            Some(" ")
        );
        assert_eq!(inspect("\u{1}", r, &options).unwrap().prepared, None);
        assert_eq!(
            normalize("a", route(Language::Fra), &options)
                .unwrap_err()
                .kind,
            ErrorKind::Internal
        );
    }

    #[test]
    fn segmentation_reconstructs_raw_utf8_and_supplements_only_missing_groups() {
        let input = "PostgreSQL 中文 café 日本語 한국어 Русский язык";
        let (spans, calls) = segment_counted(input).unwrap();
        assert_eq!(
            spans
                .iter()
                .map(|s| &input[s.start..s.end])
                .collect::<String>(),
            input
        );
        for span in &spans {
            assert_eq!(input[..span.start].chars().count(), span.char_start);
            assert_eq!(input[..span.end].chars().count(), span.char_end);
            if span.charabia_language.is_some() {
                assert_eq!(span.language_source, LanguageSource::Charabia);
            }
            assert_eq!(span.route.script(), span.script);
        }
        let groups = spans
            .iter()
            .enumerate()
            .filter(|(i, s)| {
                matches!(
                    s.script,
                    Script::Latin | Script::Cyrillic | Script::Hebrew | Script::Devanagari
                ) && (*i == 0 || spans[*i - 1].script != s.script)
            })
            .count();
        assert_eq!(calls, groups);
        let (korean, calls) = segment_counted("한국어 검색").unwrap();
        assert_eq!(calls, 0);
        assert!(
            korean
                .iter()
                .all(|s| s.route.language() == Some(Language::Kor))
        );
        let (latin, calls) = segment_counted("these cats were running in the garden").unwrap();
        assert_eq!(calls, 1);
        assert!(
            latin
                .iter()
                .all(|s| s.language_source == LanguageSource::Supplement)
        );
        assert_eq!(segment_counted("1234 !").unwrap().1, 0);
    }

    #[test]
    fn parser_prefix_classification_keeps_external_dictionaries_off_blank() {
        let input = "alpha, beta\t\u{1} CAFÉ";
        let spans = segment(input).unwrap();
        let words = spans
            .iter()
            .filter(|span| span.token_type.is_language())
            .map(|span| &input[span.start..span.end])
            .collect::<Vec<_>>();
        assert_eq!(words, ["alpha", "beta", "CAFÉ"]);
        assert!(spans.iter().any(|span| span.token_type == TokenType::Blank));
        assert!(!is_word(""));
        assert!(!is_word("\u{1}"));
        assert!(is_word("CAFÉ"));
    }

    #[test]
    fn display_fragments_preserve_long_raw_text_without_subterms() {
        for text in ["x".repeat(2046), "x".repeat(2047), "é".repeat(1024)] {
            let spans = segment(&text).unwrap();
            assert_eq!(
                spans
                    .iter()
                    .map(|s| &text[s.start..s.end])
                    .collect::<String>(),
                text
            );
            assert!(spans.iter().all(|s| s.end - s.start <= MAX_RAW_TOKEN_BYTES));
            if text.len() > MAX_RAW_TOKEN_BYTES {
                assert!(spans.iter().all(|s| s.token_type == TokenType::Display));
            } else {
                assert!(spans.iter().all(|s| s.token_type != TokenType::Display));
            }
        }
    }

    #[test]
    fn a_long_separator_run_retains_the_original_small_spans() {
        // The locked segmenter returns 4096 separate one-byte spaces, not a
        // single oversized raw token (preserved boundary-inspection evidence).
        let input = " ".repeat(4096);
        let spans = segment(&input).unwrap();
        assert_eq!(spans.len(), 4096);
        assert!(
            spans
                .iter()
                .all(|span| span.end - span.start == 1 && span.token_type != TokenType::Display)
        );
        assert_eq!(
            spans
                .iter()
                .map(|span| &input[span.start..span.end])
                .collect::<String>(),
            input
        );
    }

    #[test]
    fn raw_boundaries_and_input_limits_are_checked() {
        assert!(segment("").unwrap().is_empty());
        assert_eq!(segment("a\0b").unwrap_err().kind, ErrorKind::InvalidInput);
        assert_eq!(
            segment(&"a".repeat(MAX_INPUT_BYTES + 1)).unwrap_err().kind,
            ErrorKind::Limit
        );
        assert!(check_input(&"a".repeat(MAX_INPUT_BYTES)).is_ok());
        assert!(checked_span("中a", 0, 3, 0).is_ok());
        for (start, end, previous) in [(1, 3, 0), (0, 1, 0), (0, 0, 0), (0, 5, 0)] {
            assert_eq!(
                checked_span("中a", start, end, previous).unwrap_err().kind,
                ErrorKind::Internal
            );
        }
    }
}

#[cfg(test)]
mod resource_identity_tests {
    use super::*;
    use sha2::{Digest, Sha256};
    fn identities() -> serde_json::Value {
        let mut data = serde_json::Map::new();
        for route in LanguageId::ALL {
            let stops = crate::stopwords::build(route).unwrap();
            let mut hash = Sha256::new();
            for key in stops.fst.stream().into_strs().unwrap() {
                hash.update((key.len() as u64).to_le_bytes());
                hash.update(key.as_bytes());
            }
            data.insert(route.alias().to_owned(),serde_json::json!({"entries":stops.fst.len(),"excluded_builtin_entries":stops.excluded_builtin_count,"sha256":format!("{:x}",hash.finalize())}));
        }
        serde_json::Value::Object(data)
    }
    #[test]
    #[ignore = "explicit resource identity generation; not a semantic golden"]
    fn print_builtin_resource_identity() {
        println!("PGCB_RESOURCE_ID={}", identities());
    }
    #[test]
    fn builtin_resource_identity_matches_locked_manifest() {
        let manifest: serde_json::Value =
            serde_json::from_str(include_str!("../resources/manifest.json")).unwrap();
        assert_eq!(identities(), manifest["canonical_stop_sets"]);
    }
}

//! One-pass raw segmentation and per-route, bounded general analysis.
use std::borrow::Cow;
use std::fmt;

use charabia::normalizer::{ClassifierOption, NormalizerOption};
use charabia::{Normalize, Script, Token, TokenKind, TokenizerBuilder};
use qdrant_rust_stemmers::Stemmer;
use unicode_normalization::UnicodeNormalization;

pub use crate::language::LanguageSource;
use crate::routes::Route;

pub const MAX_INPUT_BYTES: usize = 262_144;
pub const MAX_STOP_WORDS_JSON_BYTES: usize = 1_048_576;
pub const MAX_STOP_WORDS: usize = 65_536;
pub const MAX_STOP_WORD_BYTES: usize = 2_046;
pub const MAX_STOP_KEY_BYTES: usize = 16_777_216;
pub const MAX_RAW_TOKEN_BYTES: usize = 2_046;

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
    pub route: Route,
    pub script: Script,
    pub charabia_language: Option<charabia::Language>,
    pub language_source: LanguageSource,
}

pub struct Options {
    route: Route,
    stemming: bool,
    builtin_stopwords: bool,
    stops: crate::stopwords::EffectiveStops,
    stemmer: Option<Stemmer>,
}

impl fmt::Debug for Options {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Options")
            .field("route", &self.route)
            .field("stemming", &self.stemming())
            .field("builtin_stopwords", &self.builtin_stopwords())
            .field("fst_bytes", &self.fst_bytes())
            .field("effective_stopwords", &self.effective_stopwords())
            .field("excluded_builtin_count", &self.excluded_builtin_count())
            .finish()
    }
}

impl Options {
    pub fn new(
        route: Route,
        stemming: bool,
        builtin: bool,
        add: &[String],
        keep: &[String],
    ) -> Result<Self, AnalysisError> {
        if route.is_display() {
            return Err(AnalysisError::new(
                ErrorKind::InvalidInput,
                "display-only has no dictionary",
            ));
        }
        // Match the public canonical settings budget on direct INIT as well.
        // Borrow the arrays: validation does not clone user configuration data.
        #[derive(serde::Serialize)]
        struct LocalSettings<'a> {
            builtin_stopwords: bool,
            stemming: bool,
            stopwords_add: std::collections::BTreeMap<&'a str, &'a [String]>,
            stopwords_keep: std::collections::BTreeMap<&'a str, &'a [String]>,
        }
        let mut additions = std::collections::BTreeMap::new();
        let mut retained = std::collections::BTreeMap::new();
        if !add.is_empty() {
            additions.insert(route.alias(), add);
        }
        if !keep.is_empty() {
            retained.insert(route.alias(), keep);
        }
        let local = LocalSettings {
            builtin_stopwords: builtin,
            stemming,
            stopwords_add: additions,
            stopwords_keep: retained,
        };
        let bytes = serde_json::to_vec(&local).expect("settings only contain strings and booleans");
        if bytes.len() > MAX_STOP_WORDS_JSON_BYTES {
            return Err(AnalysisError::new(
                ErrorKind::Limit,
                "dictionary settings JSON exceeds 1048576 bytes",
            ));
        }
        let stops = crate::stopwords::build(route, builtin, add, keep)?;
        Ok(Self {
            route,
            stemming,
            builtin_stopwords: builtin,
            stops,
            stemmer: stemming
                .then(|| route.algorithm().map(Stemmer::create))
                .flatten(),
        })
    }

    pub fn stemming(&self) -> bool {
        self.stemming
    }
    pub fn builtin_stopwords(&self) -> bool {
        self.builtin_stopwords
    }

    pub fn fst_bytes(&self) -> usize {
        self.stops.fst.as_fst().as_bytes().len()
    }
    pub fn effective_stopwords(&self) -> usize {
        self.stops.fst.len()
    }
    pub fn excluded_builtin_count(&self) -> usize {
        self.stops.excluded_builtin_count
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
    Stop,
    Ignore,
}

impl LexizeOutcome {
    pub fn into_terms(self) -> Vec<String> {
        match self {
            Self::Terms { first, second } => {
                let mut terms = vec![first];
                if let Some(second) = second {
                    terms.push(second);
                }
                terms
            }
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
            route: Route::UND,
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
    for span in spans {
        if span.end - span.start <= MAX_RAW_TOKEN_BYTES {
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
                route: Route::DISPLAY,
                language_source: LanguageSource::None,
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

pub fn prepare(raw: &str, route: Route) -> Result<Prepared, AnalysisError> {
    check_input(raw)?;
    if route.is_display() {
        return Err(AnalysisError::new(
            ErrorKind::InvalidInput,
            "display-only has no prepared key",
        ));
    }
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

fn finalize(key: &str, route: Route) -> String {
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

pub fn inspect(raw: &str, route: Route, options: &Options) -> Result<TokenAnalysis, AnalysisError> {
    if route != options.route {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "dictionary options belong to another route",
        ));
    }
    let Prepared { kind, key } = prepare(raw, route)?;
    if kind != TokenKind::Word || key.is_empty() {
        return Ok(TokenAnalysis {
            prepared: (!key.is_empty()).then_some(key),
            kind,
            outcome: LexizeOutcome::Ignore,
        });
    }
    if options.stops.fst.contains(&key) {
        return Ok(TokenAnalysis {
            prepared: Some(key),
            kind: TokenKind::StopWord,
            outcome: LexizeOutcome::Stop,
        });
    }
    let surface = finalize(&key, route);
    let stem = if key
        .chars()
        .all(|c| c.is_alphabetic() || unicode_normalization::char::is_combining_mark(c))
    {
        options.stemmer.as_ref().map(|stemmer| stemmer.stem(&key))
    } else {
        None
    };
    let stem = stem.as_ref().and_then(|stem| {
        if stem.as_ref() == key {
            return None;
        }
        let term = finalize(stem, route);
        (!term.is_empty() && term != surface).then_some(term)
    });
    let outcome = if surface.is_empty() {
        stem.map_or(LexizeOutcome::Ignore, |first| LexizeOutcome::Terms {
            first,
            second: None,
        })
    } else {
        LexizeOutcome::Terms {
            first: surface,
            second: stem,
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
    route: Route,
    options: &Options,
) -> Result<LexizeOutcome, AnalysisError> {
    inspect(raw, route, options).map(|token| token.outcome)
}

#[cfg(test)]
mod tests {
    use super::*;
    use charabia::Language;

    fn route(language: Language) -> Route {
        Route::from_language(Some(language)).unwrap()
    }
    fn terms(raw: &str, language: Language, stemming: bool) -> Vec<String> {
        let route = route(language);
        normalize(
            raw,
            route,
            &Options::new(route, stemming, false, &[], &[]).unwrap(),
        )
        .unwrap()
        .into_terms()
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
    fn surface_is_retained_and_stemming_stop_switches_are_independent() {
        let r = route(Language::Eng);
        let on = Options::new(r, true, true, &[], &[]).unwrap();
        let no_stem = Options::new(r, false, true, &[], &[]).unwrap();
        let no_builtin = Options::new(r, true, false, &["custom".into()], &[]).unwrap();
        let keep = Options::new(r, true, true, &[], &["running".into(), "THE".into()]).unwrap();
        assert_eq!(normalize("the", r, &on).unwrap(), LexizeOutcome::Stop);
        assert_eq!(normalize("the", r, &no_stem).unwrap(), LexizeOutcome::Stop);
        assert_eq!(
            normalize("running", r, &no_stem).unwrap().into_terms(),
            ["running"]
        );
        assert_eq!(
            normalize("custom", r, &no_builtin).unwrap(),
            LexizeOutcome::Stop
        );
        assert_eq!(
            normalize("the", r, &no_builtin).unwrap().into_terms(),
            ["the"]
        );
        assert_eq!(
            normalize("running", r, &keep).unwrap().into_terms(),
            ["running", "run"]
        );
        assert_eq!(normalize("THE", r, &keep).unwrap().into_terms(), ["the"]);
        assert_eq!(terms("run", Language::Eng, true), ["run"]);
        assert_eq!(terms("123", Language::Eng, true), ["123"]);
        assert_eq!(terms("foo_bar", Language::Eng, true), ["foo_bar"]);
    }

    #[test]
    fn empty_keys_and_separators_are_ignore_before_stop() {
        let r = route(Language::Eng);
        let options = Options::new(r, true, true, &[], &[]).unwrap();
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
                assert!(spans.iter().all(
                    |s| s.route == Route::DISPLAY && s.language_source == LanguageSource::None
                ));
            } else {
                assert!(spans.iter().all(|s| !s.route.is_display()));
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
                .all(|span| span.end - span.start == 1 && !span.route.is_display())
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
        for route in Route::ALL.into_iter().filter(|r| !r.is_display()) {
            let options = Options::new(route, false, true, &[], &[]).unwrap();
            let mut hash = Sha256::new();
            for key in options.stops.fst.stream().into_strs().unwrap() {
                hash.update((key.len() as u64).to_le_bytes());
                hash.update(key.as_bytes());
            }
            data.insert(route.alias().to_owned(),serde_json::json!({"entries":options.effective_stopwords(),"excluded_builtin_entries":options.excluded_builtin_count(),"sha256":format!("{:x}",hash.finalize())}));
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

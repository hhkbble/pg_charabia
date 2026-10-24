//! One-pass raw segmentation and independent, bounded dictionary analysis.
use std::fmt;

use charabia::{Script, TokenKind, TokenizerBuilder};
use qdrant_rust_stemmers::Stemmer;
use unicode_normalization::UnicodeNormalization;

pub use crate::language::LanguageSource;
use crate::prepare::finalize;
pub use crate::prepare::{
    AnalysisError, ErrorKind, MAX_INPUT_BYTES, MAX_RAW_TOKEN_BYTES, MAX_STEM_BYTES, Prepared,
    check_input, is_word, prepare,
};
use crate::routes::{LanguageId, TokenType};

#[cfg(test)]
thread_local! {
    static STRUCTURED_SCAN_CALLS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// The Parser chooses a mode before segmentation; dictionary state is shared.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AnalysisMode {
    Linguistic,
    Structured,
}

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
    english: bool,
    stops: Option<&'static crate::resources::StopSet>,
    english_stops: Option<&'static crate::resources::StopSet>,
    stemmer: Option<Stemmer>,
    english_stemmer: Option<Stemmer>,
}

impl fmt::Debug for Options {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Options")
            .field("language", &self.language)
            .field("discard", &self.discard)
            .field("stem", &self.stem)
            .field("normalize", &self.normalize)
            .field("filter", &self.filter)
            .field("english", &self.english)
            .field("owned_fst_bytes", &self.fst_bytes())
            .field("referenced_fst_bytes", &self.referenced_fst_bytes())
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
        english: bool,
    ) -> Result<Self, AnalysisError> {
        if english && (!language.english_eligible() || filter) {
            return Err(AnalysisError::new(
                ErrorKind::InvalidInput,
                "English requires an eligible Latin language and Filter=false",
            ));
        }
        if filter && stem {
            return Err(AnalysisError::new(
                ErrorKind::InvalidInput,
                "Filter requires Stem=false",
            ));
        }
        if discard && !language.has_stopwords() {
            return Err(AnalysisError::new(
                ErrorKind::InvalidInput,
                "language has no builtin stopword table",
            ));
        }
        if stem && language.algorithm().is_none() && !english {
            return Err(AnalysisError::new(
                ErrorKind::InvalidInput,
                "language has no active stemming algorithm",
            ));
        }
        Ok(Self {
            language,
            discard,
            stem,
            normalize,
            filter,
            english,
            stops: discard
                .then(|| crate::stopwords::stop_set(language))
                .flatten(),
            english_stops: (discard && english)
                .then(|| crate::stopwords::stop_set(LanguageId::ENG))
                .flatten(),
            stemmer: stem
                .then(|| language.algorithm().map(Stemmer::create))
                .flatten(),
            english_stemmer: (stem && english)
                .then(|| Stemmer::create(qdrant_rust_stemmers::Algorithm::English)),
        })
    }

    pub fn language(&self) -> LanguageId {
        self.language
    }
    /// Dictionary states borrow immutable FST bytes; reset owns none of them.
    pub fn fst_bytes(&self) -> usize {
        0
    }
    pub fn referenced_fst_bytes(&self) -> usize {
        [self.stops, self.english_stops]
            .into_iter()
            .flatten()
            .map(|set| set.fst.as_fst().as_bytes().len())
            .sum()
    }
    pub fn effective_stopwords(&self) -> usize {
        [self.stops, self.english_stops]
            .into_iter()
            .flatten()
            .map(|set| set.fst.len())
            .sum()
    }
    pub fn excluded_builtin_count(&self) -> usize {
        [self.stops, self.english_stops]
            .into_iter()
            .flatten()
            .map(|set| set.excluded_builtin_count)
            .sum()
    }
}

/// One to four distinct, nonempty alternatives at a single PG position.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Terms {
    terms: [String; 4],
    len: usize,
}

impl Terms {
    fn new(first: String) -> Option<Self> {
        if first.is_empty() {
            return None;
        }
        Some(Self {
            terms: [first, String::new(), String::new(), String::new()],
            len: 1,
        })
    }
    fn add(&mut self, term: String) {
        if !term.is_empty() && !self.iter().any(|existing| existing == term) {
            // Only two branches, each with surface plus one stem, reach here.
            assert!(
                self.len < self.terms.len(),
                "more than four dictionary terms"
            );
            self.terms[self.len] = term;
            self.len += 1;
        }
    }
    pub fn iter(&self) -> impl ExactSizeIterator<Item = &str> {
        self.terms[..self.len].iter().map(String::as_str)
    }
    pub fn len(&self) -> usize {
        self.len
    }
}

impl IntoIterator for Terms {
    type Item = String;
    type IntoIter = std::iter::Take<std::array::IntoIter<String, 4>>;
    fn into_iter(self) -> Self::IntoIter {
        self.terms.into_iter().take(self.len)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LexizeOutcome {
    Terms(Terms),
    Filter(String),
    Stop,
    Ignore,
}

impl LexizeOutcome {
    #[cfg(test)]
    pub fn into_terms(self) -> Vec<String> {
        match self {
            Self::Terms(terms) => terms.into_iter().collect(),
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

pub fn segment(input: &str, mode: AnalysisMode) -> Result<Vec<Span>, AnalysisError> {
    segment_counted(input, mode).map(|(spans, _)| spans)
}

pub(crate) fn segment_counted(
    input: &str,
    mode: AnalysisMode,
) -> Result<(Vec<Span>, usize), AnalysisError> {
    check_input(input)?;
    let allowed = crate::language_data::LANGUAGES.map(|row| row.0);
    let mut builder = TokenizerBuilder::default();
    builder.allow_list(&allowed);
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
    let spans = match mode {
        AnalysisMode::Linguistic => spans,
        AnalysisMode::Structured => {
            #[cfg(test)]
            STRUCTURED_SCAN_CALLS.with(|calls| calls.set(calls.get() + 1));
            let candidates = crate::structured_tokens::recognition::scan(input);
            overlay_structured_tokens(input, spans, &candidates)?
        }
    };
    let spans = split_literal_delimiters(input, spans);
    // Classify each upstream span once. The merge and type assignment reuse it.
    let classified = spans
        .into_iter()
        .map(|span| {
            let raw = &input[span.start..span.end];
            let word = !matches!(raw, "<" | ">" | "=")
                && !matches!(span.token_type, TokenType::Structured(_))
                && is_word(&input[span.start..span.end]);
            (span, word)
        })
        .collect();
    let restored = restore_english(input, classified, crate::resources::english_contains);
    let mut bounded = Vec::with_capacity(restored.len());
    for (mut span, word) in restored {
        if span.end - span.start <= MAX_RAW_TOKEN_BYTES {
            span.token_type = if matches!(span.token_type, TokenType::Structured(_)) {
                span.token_type
            } else if word {
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

fn split_literal_delimiters(input: &str, spans: Vec<Span>) -> Vec<Span> {
    if !input.contains(['<', '>', '=']) {
        return spans;
    }
    let mut output = Vec::with_capacity(spans.len());
    for span in spans {
        if matches!(span.token_type, TokenType::Structured(_)) {
            output.push(span);
            continue;
        }
        let raw = &input[span.start..span.end];
        let (mut byte, mut scalar) = (span.start, span.char_start);
        for (offset, character) in raw.char_indices() {
            if !matches!(character, '<' | '>' | '=') {
                continue;
            }
            let delimiter = span.start + offset;
            if byte < delimiter {
                let char_end = scalar + input[byte..delimiter].chars().count();
                output.push(Span {
                    start: byte,
                    end: delimiter,
                    char_start: scalar,
                    char_end,
                    ..span
                });
                scalar = char_end;
            }
            output.push(Span {
                start: delimiter,
                end: delimiter + 1,
                char_start: scalar,
                char_end: scalar + 1,
                ..span
            });
            byte = delimiter + 1;
            scalar += 1;
        }
        if byte < span.end {
            output.push(Span {
                start: byte,
                char_start: scalar,
                ..span
            });
        }
    }
    output
}

fn overlay_structured_tokens(
    input: &str,
    spans: Vec<Span>,
    candidates: &[crate::structured_tokens::Candidate],
) -> Result<Vec<Span>, AnalysisError> {
    if candidates.is_empty() {
        return Ok(spans);
    }
    let mut output = Vec::with_capacity(spans.len() + candidates.len());
    let (mut byte, mut scalar, mut upstream, mut candidate) = (0, 0, 0, 0);
    while byte < input.len() {
        while upstream < spans.len() && spans[upstream].end <= byte {
            upstream += 1;
        }
        let span = *spans.get(upstream).ok_or_else(|| {
            AnalysisError::new(ErrorKind::Internal, "symbol overlay lost the original span")
        })?;
        let next = candidates.get(candidate);
        let structured = next.filter(|item| item.start == byte);
        let end = if let Some(item) = structured {
            item.end
        } else {
            next.map_or(span.end, |item| span.end.min(item.start))
        };
        let raw = checked_span(input, byte, end, byte)?;
        let char_end = scalar + raw.chars().count();
        let mut clipped = Span {
            start: byte,
            end,
            char_start: scalar,
            char_end,
            ..span
        };
        if let Some(item) = structured {
            clipped.token_type = TokenType::Structured(item.kind);
            clipped.route = LanguageId::UND;
            clipped.language_source = LanguageSource::None;
            // A structure may cover several upstream metadata records.
            clipped.charabia_language = None;
            candidate += 1;
        }
        output.push(clipped);
        byte = end;
        scalar = char_end;
    }
    if candidate != candidates.len() {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "invalid symbol overlay range",
        ));
    }
    Ok(output)
}

/// Restore only a complete split ASCII envelope; never search its substrings.
fn restore_english(
    input: &str,
    spans: Vec<(Span, bool)>,
    mut contains: impl FnMut(&str) -> bool,
) -> Vec<(Span, bool)> {
    let mut output = Vec::with_capacity(spans.len());
    let mut first = 0;
    while first < spans.len() {
        let mut end = first + 1;
        if spans[first].1 {
            while end < spans.len() && spans[end].1 && spans[end - 1].0.end == spans[end].0.start {
                end += 1;
            }
        }
        let span = spans[first].0;
        let last = spans[end - 1].0;
        let raw = &input[span.start..last.end];
        let same_metadata = spans[first..end].iter().all(|(current, _)| {
            current.script == span.script && current.charabia_language == span.charabia_language
        });
        let attached =
            |c: char| c.is_alphanumeric() || unicode_normalization::char::is_combining_mark(c);
        let is_attached = input[..span.start]
            .chars()
            .next_back()
            .is_some_and(attached)
            || input[last.end..].chars().next().is_some_and(attached);
        if end - first >= 2
            && same_metadata
            && !is_attached
            && raw.bytes().all(|byte| byte.is_ascii_alphabetic())
            && contains(&raw.to_ascii_lowercase())
        {
            output.push((
                Span {
                    end: last.end,
                    char_end: last.char_end,
                    route: LanguageId::ENG,
                    language_source: LanguageSource::EnglishMerge,
                    ..span
                },
                true,
            ));
        } else {
            output.extend_from_slice(&spans[first..end]);
        }
        first = end;
    }
    output
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

pub fn stem_eligible(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= MAX_STEM_BYTES
        && key
            .chars()
            .all(|c| c.is_alphabetic() || unicode_normalization::char::is_combining_mark(c))
}

fn branch(
    raw: &str,
    language: LanguageId,
    options: &Options,
    stops: Option<&crate::resources::StopSet>,
    stemmer: Option<&Stemmer>,
) -> Result<TokenAnalysis, AnalysisError> {
    let Prepared { kind, key } = prepare(raw, language)?;
    if kind != TokenKind::Word || key.is_empty() {
        return Ok(TokenAnalysis {
            prepared: (!key.is_empty()).then_some(key),
            kind,
            outcome: LexizeOutcome::Ignore,
        });
    }
    if stops.is_some_and(|set| set.fst.contains(&key)) {
        return Ok(TokenAnalysis {
            prepared: Some(key),
            kind: TokenKind::StopWord,
            outcome: LexizeOutcome::Stop,
        });
    }
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
        let stem = stemmer
            .filter(|_| stem_eligible(&key))
            .map(|stemmer| stemmer.stem(&key))
            // An unchanged stem has the same finalized spelling as the surface.
            .filter(|stem| stem.as_ref() != key)
            .map(|stem| {
                if options.normalize {
                    finalize(&stem, language)
                } else {
                    stem.nfc().collect()
                }
            });
        let mut terms = Terms::new(surface);
        if let Some(stem) = stem {
            if let Some(terms) = &mut terms {
                terms.add(stem);
            } else {
                terms = Terms::new(stem);
            }
        }
        terms.map_or(LexizeOutcome::Ignore, LexizeOutcome::Terms)
    };
    Ok(TokenAnalysis {
        prepared: Some(key),
        kind,
        outcome,
    })
}

/// Analyze an already bounded English leaf without tokenizing or detecting it.
pub(crate) fn english_component(
    raw: &str,
    options: &Options,
) -> Result<Vec<String>, AnalysisError> {
    let result = branch(
        raw,
        LanguageId::ENG,
        options,
        options.stops,
        options.stemmer.as_ref(),
    )?;
    Ok(match result.outcome {
        LexizeOutcome::Terms(terms) => terms.into_iter().collect(),
        LexizeOutcome::Stop | LexizeOutcome::Ignore => Vec::new(),
        LexizeOutcome::Filter(_) => unreachable!("symbol English options are terminal"),
    })
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
    // Direct LEXIZE and earlier filters may supply bytes never seen by Parser.
    let mut native = branch(
        raw,
        language,
        options,
        options.stops,
        options.stemmer.as_ref(),
    )?;
    if options.english
        && !raw.is_empty()
        && raw.bytes().all(|byte| byte.is_ascii_alphabetic())
        && crate::resources::english_contains(&raw.to_ascii_lowercase())
    {
        let english = branch(
            raw,
            LanguageId::ENG,
            options,
            options.english_stops,
            options.english_stemmer.as_ref(),
        )?;
        match english.outcome {
            LexizeOutcome::Terms(english_terms) => {
                match &mut native.outcome {
                    LexizeOutcome::Terms(native_terms) => {
                        for term in english_terms {
                            native_terms.add(term);
                        }
                    }
                    _ => native.outcome = LexizeOutcome::Terms(english_terms),
                }
                native.kind = TokenKind::Word;
            }
            LexizeOutcome::Stop if !matches!(native.outcome, LexizeOutcome::Terms(_)) => {
                native.kind = TokenKind::StopWord;
                native.outcome = LexizeOutcome::Stop;
            }
            LexizeOutcome::Stop => {}
            LexizeOutcome::Ignore | LexizeOutcome::Filter(_) => {
                unreachable!("an active ASCII English branch is Terms or Stop")
            }
        }
    }
    Ok(native)
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
    fn options(
        language: LanguageId,
        discard: bool,
        stem: bool,
        normalize: bool,
        filter: bool,
        english: bool,
    ) -> Options {
        Options::new(language, discard, stem, normalize, filter, english).unwrap()
    }
    fn terms(raw: &str, language: Language, stemming: bool) -> Vec<String> {
        let language = route(language);
        normalize(
            raw,
            language,
            &options(language, false, stemming, true, false, false),
        )
        .unwrap()
        .into_terms()
    }

    #[test]
    fn fixed_language_capabilities_bound_the_boolean_state_space() {
        // The independent conditions enumerate option validity, not detector quality.
        let mut groups = [0usize; 5];
        let mut legal = 0;
        let mut defaults = [0usize; 3];
        for language in LanguageId::ALL {
            let stop = language.has_stopwords();
            let stem = language.algorithm().is_some();
            let eligible = language.english_eligible();
            let group = match (eligible, stop, stem) {
                (true, true, true) => 0,
                (true, true, false) => 1,
                (false, true, true) => 2,
                (false, true, false) => 3,
                (false, false, false) => 4,
                _ => panic!("unreachable capability combination"),
            };
            groups[group] += 1;
            defaults[0] += usize::from(stop);
            defaults[1] += usize::from(language.default_stem(language.default_english(false)));
            defaults[2] += usize::from(language.default_english(false));
            for bits in 0..32 {
                let (d, s, n, f, e) = (
                    bits & 1 != 0,
                    bits & 2 != 0,
                    bits & 4 != 0,
                    bits & 8 != 0,
                    bits & 16 != 0,
                );
                let expected =
                    (!e || eligible) && !(e && f) && !(s && f) && (!d || stop) && (!s || stem || e);
                let actual = Options::new(language, d, s, n, f, e);
                assert_eq!(actual.is_ok(), expected, "{}: {bits}", language.alias());
                legal += usize::from(expected);
                if let Ok(options) = actual {
                    assert_eq!(options.stops.is_some(), d);
                    assert_eq!(options.english_stops.is_some(), d && e);
                    assert_eq!(options.fst_bytes(), 0);
                }
            }
        }
        assert_eq!(groups, [7, 1, 3, 4, 1]);
        assert_eq!(defaults, [15, 11, 8]);
        assert_eq!(legal, 228);
    }

    #[test]
    fn independent_algorithm_goldens_preserve_pre_stem_unicode() {
        // English is the crate's documented sample. Accented French/Turkish
        // outputs are retained direct upstream runs, not generated expectations.
        assert_eq!(
            terms("fruitlessly", Language::Eng, true),
            ["fruitlessly", "fruitless"]
        );
        assert_eq!(terms("aimées", Language::Fra, true), ["aimees", "aim"]);
        assert_eq!(terms("IŞIKLAR", Language::Tur, true), ["isiklar", "isik"]);
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
        let r = LanguageId::ENG;
        let on = options(r, true, true, true, false, false);
        let no_stem = options(r, true, false, true, false, false);
        let no_stop = options(r, false, true, true, false, false);
        let no_fold = options(r, true, true, false, false, false);
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
        assert_eq!(on.fst_bytes(), 0);
        assert!(on.referenced_fst_bytes() > 0);
        assert_eq!(no_stop.referenced_fst_bytes(), 0);
        assert_eq!(no_stop.effective_stopwords(), 0);
        assert_eq!(terms("run", Language::Eng, true), ["run"]);
        assert_eq!(terms("123", Language::Eng, true), ["123"]);
        assert_eq!(terms("foo_bar", Language::Eng, true), ["foo_bar"]);
    }

    #[test]
    fn unchanged_stems_keep_one_surface_with_either_normalization_setting() {
        let language = LanguageId::ENG;
        for (fold, expected) in [(false, "café"), (true, "cafe")] {
            for stem in [false, true] {
                assert_eq!(
                    normalize(
                        "CAFÉ",
                        language,
                        &options(language, false, stem, fold, false, false)
                    )
                    .unwrap()
                    .into_terms(),
                    [expected]
                );
            }
            assert_eq!(
                normalize(
                    "running",
                    language,
                    &options(language, false, true, fold, false, false)
                )
                .unwrap()
                .into_terms(),
                ["running", "run"]
            );
        }
    }

    #[test]
    fn normalize_false_preserves_prepared_accents_and_nfc_stems() {
        for (language, raw, expected) in [
            (Language::Fra, "aimées", ["aimées", "aim"]),
            (Language::Tur, "IŞIKLAR", ["ışıklar", "ışık"]),
        ] {
            let r = route(language);
            assert_eq!(
                normalize(raw, r, &options(r, false, true, false, false, false))
                    .unwrap()
                    .into_terms(),
                expected
            );
        }
        for (language, raw, plain, folded) in [
            (LanguageId::UND, "CAFÉ", "café", "cafe\u{301}"),
            (route(Language::Jpn), "ダメ", "ダメ", "た\u{3099}め"),
        ] {
            for (fold, expected) in [(false, plain), (true, folded)] {
                assert_eq!(
                    normalize(
                        raw,
                        language,
                        &options(language, false, false, fold, false, false)
                    )
                    .unwrap()
                    .into_terms(),
                    [expected]
                );
            }
        }
    }

    #[test]
    fn english_branch_preserves_native_terms_and_aggregates_stops() {
        let r = route(Language::Deu);
        let with_english = options(r, true, true, true, false, true);
        let native = options(r, true, true, true, false, false);
        assert_eq!(normalize("die", r, &native).unwrap(), LexizeOutcome::Stop);
        // This is the explicit parallel-branch contract, not a claim that the
        // corresponding German-domain false positive has been fixed.
        assert_eq!(
            normalize("die", r, &with_english).unwrap().into_terms(),
            ["die"]
        );
        assert_eq!(
            normalize("in", r, &with_english).unwrap(),
            LexizeOutcome::Stop
        );
        assert_eq!(
            normalize("the", r, &with_english).unwrap().into_terms(),
            ["the"]
        );
        assert_eq!(
            normalize(" ", r, &with_english).unwrap(),
            LexizeOutcome::Ignore
        );
        let r = route(Language::Vie);
        let e = options(r, false, true, true, false, true);
        assert_eq!(
            normalize("running", r, &e).unwrap().into_terms(),
            ["running", "run"]
        );
        assert!(Options::new(r, false, true, true, false, false).is_err());
        let r = route(Language::Tur);
        let e = options(r, true, false, true, false, true);
        assert_eq!(normalize("i", r, &e).unwrap().into_terms(), ["i"]);
    }

    #[test]
    fn four_alternatives_have_native_then_english_order_and_final_deduplication() {
        let r = route(Language::Tur);
        let raw = "ABBREVIATE";
        assert!(crate::resources::english_contains("abbreviate"));
        let outcome = normalize(raw, r, &options(r, false, true, false, false, true)).unwrap();
        if let LexizeOutcome::Terms(terms) = &outcome {
            assert_eq!(terms.len(), 4);
            assert_eq!(
                terms.iter().collect::<Vec<_>>(),
                ["abbrevıate", "abbrevıa", "abbreviate", "abbrevi"]
            );
        } else {
            panic!("expected four independent alternatives");
        }
        assert_eq!(
            outcome.into_terms(),
            ["abbrevıate", "abbrevıa", "abbreviate", "abbrevi"]
        );
        assert_eq!(
            normalize(raw, r, &options(r, false, true, true, false, true))
                .unwrap()
                .into_terms(),
            ["abbreviate", "abbrevia", "abbrevi"]
        );
        let r = route(Language::Fra);
        assert_eq!(
            normalize("running", r, &options(r, false, false, true, false, true))
                .unwrap()
                .into_terms(),
            ["running"]
        );
    }

    #[test]
    fn english_gate_uses_current_raw_ascii_bytes_and_not_prepared_ascii() {
        let r = route(Language::Tur);
        let native = options(r, false, true, false, false, false);
        let english = options(r, false, true, false, false, true);
        for raw in ["İSTANBUL", "ﬃ", "two words", "running_", "running42", ""] {
            assert_eq!(
                normalize(raw, r, &native).unwrap(),
                normalize(raw, r, &english).unwrap(),
                "{raw:?}"
            );
        }
    }

    #[test]
    fn filter_replaces_one_term_and_stop_remains_terminal() {
        let r = LanguageId::ENG;
        assert!(Options::new(r, false, true, true, true, false).is_err());
        let folded = options(r, true, false, true, true, false);
        let prepared = options(r, false, false, false, true, false);
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
        assert!(Options::new(route(Language::Fra), false, false, true, true, true).is_err());
        assert!(Options::new(LanguageId::UND, true, false, true, false, false).is_err());
        assert!(Options::new(LanguageId::UND, false, true, true, false, false).is_err());
        assert!(Options::new(LanguageId::UND, false, false, true, true, false).is_ok());
    }

    #[test]
    fn actual_prepared_stem_input_is_bounded() {
        let r = LanguageId::ENG;
        let options = options(r, false, true, false, false, false);
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
        let expanded = "ﬃ".repeat(334);
        assert_eq!(prepare(&expanded, r).unwrap().key.len(), 1002);
        assert!(!stem_eligible(&prepare(&expanded, r).unwrap().key));
        for invalid in ["", "123", "word_part", "two words"] {
            assert!(!stem_eligible(invalid));
        }
    }

    #[test]
    fn empty_keys_and_separators_are_ignore_before_stop() {
        let r = LanguageId::ENG;
        let options = options(r, true, true, true, false, false);
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

    fn classified(pieces: &[&str]) -> (String, Vec<(Span, bool)>) {
        let input = pieces.concat();
        let mut start = 0;
        let mut char_start = 0;
        let spans = pieces
            .iter()
            .map(|raw| {
                let span = Span {
                    start,
                    end: start + raw.len(),
                    char_start,
                    char_end: char_start + raw.chars().count(),
                    route: route(Language::Nld),
                    token_type: TokenType::Blank,
                    script: Script::Latin,
                    charabia_language: Some(Language::Nld),
                    language_source: LanguageSource::Charabia,
                };
                start = span.end;
                char_start = span.char_end;
                (span, is_word(raw))
            })
            .collect();
        (input, spans)
    }

    #[test]
    fn whole_envelope_gate_has_one_lookup_and_never_forces_an_unsplit_word() {
        for pieces in [
            vec!["Postgre", "SQL"],
            vec!["camel", "Case"],
            vec!["CAMEL", "Case"],
        ] {
            let (input, spans) = classified(&pieces);
            let original = spans[0].0;
            let mut calls = 0;
            let merged = restore_english(&input, spans, |key| {
                calls += 1;
                assert_eq!(key, input.to_ascii_lowercase());
                true
            });
            assert_eq!(calls, 1);
            assert_eq!(merged.len(), 1);
            assert_eq!((merged[0].0.start, merged[0].0.end), (0, input.len()));
            assert_eq!(merged[0].0.char_end, input.chars().count());
            assert_eq!(merged[0].0.route, LanguageId::ENG);
            assert_eq!(merged[0].0.language_source, LanguageSource::EnglishMerge);
            assert_eq!(merged[0].0.charabia_language, original.charabia_language);
            assert_eq!(merged[0].0.script, original.script);
        }
        let (input, spans) = classified(&["postgresql"]);
        let expected = spans.clone();
        assert_eq!(
            restore_english(&input, spans, |_| panic!("unsplit words do not look up")),
            expected
        );
        let (input, spans) = classified(&["database", "API"]);
        let expected = spans.clone();
        let mut calls = 0;
        assert_eq!(
            restore_english(&input, spans, |_| {
                calls += 1;
                false
            }),
            expected
        );
        assert_eq!(calls, 1);
    }

    #[test]
    fn whole_envelope_gate_never_deletes_nonletters_or_searches_substrings() {
        for pieces in [
            vec!["post", " ", "gre", "SQL"],
            vec!["post", "-", "gre", "SQL"],
            vec!["Postgre", "SQL", "42"],
            vec!["é", "Postgre", "SQL"],
            vec!["e", "\u{301}", "Postgre", "SQL"],
            vec!["Postgre", "SQL", "\u{301}"],
            vec!["unknown", "Postgre", "SQL", "suffix"],
        ] {
            let (input, spans) = classified(&pieces);
            let expected = spans.clone();
            let mut keys = Vec::new();
            let actual = restore_english(&input, spans, |key| {
                keys.push(key.to_owned());
                key == "postgresql"
            });
            assert_eq!(actual, expected, "{input:?}, looked up {keys:?}");
            assert!(!keys.iter().any(|key| key == "postgresql"));
        }
        let (input, mut spans) = classified(&["Postgre", "SQL"]);
        spans[1].0.charabia_language = Some(Language::Eng);
        let expected = spans.clone();
        assert_eq!(
            restore_english(&input, spans, |_| panic!("metadata mismatch")),
            expected
        );
    }

    #[test]
    fn segmentation_reconstructs_raw_utf8_without_supplementary_detection() {
        for input in [
            "PostgreSQL 中文 café 日本語 한국어 Русский язык ภาษาไทย",
            "a\u{1D2B}Бb\u{1D78}",
            "aաաաБաաա",
            "Ελληνικά עברית বাংলা हिन्दी",
            "1234 !",
        ] {
            let (spans, calls) = segment_counted(input, AnalysisMode::Structured).unwrap();
            assert_eq!(calls, 0);
            assert_eq!(
                spans
                    .iter()
                    .map(|s| &input[s.start..s.end])
                    .collect::<String>(),
                input
            );
            for span in spans {
                assert_eq!(input[..span.start].chars().count(), span.char_start);
                assert_eq!(input[..span.end].chars().count(), span.char_end);
                assert!(
                    span.route == LanguageId::UND
                        || span.route == LanguageId::ENG
                            && span.language_source == LanguageSource::EnglishMerge
                        || span.route.script() == span.script
                );
            }
        }
        for (input, language) in [
            ("한국어 검색", Language::Kor),
            ("Русский язык", Language::Rus),
            ("ภาษาไทย", Language::Tha),
        ] {
            let spans = segment(input, AnalysisMode::Structured).unwrap();
            assert!(
                spans
                    .iter()
                    .filter(|span| span.token_type.is_language())
                    .all(|span| span.route == route(language))
            );
        }
    }

    #[test]
    fn actual_camel_segmentation_and_fixed_lexicon_restore_only_the_full_word() {
        for raw in ["PostgreSQL", "postgreSQL", "camelCase"] {
            let spans = segment(raw, AnalysisMode::Structured).unwrap();
            assert_eq!(spans.len(), 1, "{raw:?}");
            assert_eq!((spans[0].start, spans[0].end), (0, raw.len()));
            assert_eq!(spans[0].route, LanguageId::ENG);
            assert_eq!(spans[0].language_source, LanguageSource::EnglishMerge);
        }
        for raw in ["postgresql", "POSTGRESQL"] {
            let spans = segment(raw, AnalysisMode::Structured).unwrap();
            assert!(
                spans
                    .iter()
                    .all(|span| span.language_source != LanguageSource::EnglishMerge)
            );
        }
        assert!(!crate::resources::english_contains("databaseapi"));
        let raw = "databaseAPI";
        let spans = segment(raw, AnalysisMode::Structured).unwrap();
        assert_eq!(
            spans
                .iter()
                .map(|span| &raw[span.start..span.end])
                .collect::<Vec<_>>(),
            ["database", "API"]
        );
        assert!(
            spans
                .iter()
                .all(|span| span.language_source != LanguageSource::EnglishMerge)
        );
    }

    #[test]
    fn linguistic_never_invokes_the_structured_scanner() {
        let input = "https://Example.com/Running?q=100ms foo_bar.rs 100ms";
        STRUCTURED_SCAN_CALLS.with(|calls| calls.set(0));
        segment(input, AnalysisMode::Linguistic).unwrap();
        assert_eq!(STRUCTURED_SCAN_CALLS.with(|calls| calls.get()), 0);
        segment(input, AnalysisMode::Structured).unwrap();
        assert_eq!(STRUCTURED_SCAN_CALLS.with(|calls| calls.get()), 1);
    }

    #[test]
    fn linguistic_keeps_ordinary_units_and_shared_raw_boundaries() {
        let input = "alpha 100ms foo_bar.rs beta";
        let spans = segment(input, AnalysisMode::Linguistic).unwrap();
        let words: Vec<_> = spans
            .iter()
            .filter(|span| span.token_type.is_language())
            .map(|span| &input[span.start..span.end])
            .collect();
        // Ordinary final Parser units, independently specified from the text.
        assert_eq!(words, ["alpha", "100ms", "foo", "bar", "rs", "beta"]);
        assert!(
            spans
                .iter()
                .all(|span| !matches!(span.token_type, TokenType::Structured(_)))
        );
        assert_eq!(
            spans
                .iter()
                .map(|s| &input[s.start..s.end])
                .collect::<String>(),
            input
        );
        for span in spans {
            assert_eq!(input[..span.start].chars().count(), span.char_start);
            assert_eq!(input[..span.end].chars().count(), span.char_end);
        }
        let structured = segment(input, AnalysisMode::Structured).unwrap();
        let kinds: Vec<_> = structured
            .iter()
            .filter_map(|span| match span.token_type {
                TokenType::Structured(kind) => Some((kind.alias(), &input[span.start..span.end])),
                _ => None,
            })
            .collect();
        assert_eq!(kinds, [("numword", "100ms"), ("file", "foo_bar.rs")]);
    }

    #[test]
    fn modes_share_language_english_delimiter_and_resource_rules() {
        for input in [
            "PostgreSQL 中文 café 日本語 한국어 Русский язык ภาษาไทย",
            "camelCase databaseAPI",
            "alpha <beta> gamma=delta",
            "a\u{1D2B}Бb\u{1D78}",
        ] {
            assert_eq!(
                segment(input, AnalysisMode::Linguistic).unwrap(),
                segment(input, AnalysisMode::Structured).unwrap(),
                "no structured candidate in {input:?}"
            );
        }
        for mode in [AnalysisMode::Linguistic, AnalysisMode::Structured] {
            assert!(segment("", mode).unwrap().is_empty());
            assert_eq!(
                segment("a\0b", mode).unwrap_err().kind,
                ErrorKind::InvalidInput
            );
            assert_eq!(
                segment(&"a".repeat(MAX_INPUT_BYTES + 1), mode)
                    .unwrap_err()
                    .kind,
                ErrorKind::Limit
            );
            let input = "é".repeat(1024);
            let spans = segment(&input, mode).unwrap();
            assert!(
                spans
                    .iter()
                    .all(|span| span.token_type == TokenType::Display)
            );
            assert_eq!(
                spans
                    .iter()
                    .map(|span| &input[span.start..span.end])
                    .collect::<String>(),
                input
            );
        }
    }

    #[test]
    fn pinned_feature_map_has_exactly_the_proved_missing_paths() {
        use charabia::segmenter::SEGMENTERS;
        let expected = [
            (Script::Latin, None),
            (Script::Latin, Some(Language::Deu)),
            (Script::Cj, Some(Language::Cmn)),
            (Script::Cj, Some(Language::Zho)),
            (Script::Cj, Some(Language::Jpn)),
            (Script::Hangul, Some(Language::Kor)),
            (Script::Thai, Some(Language::Tha)),
            (Script::Arabic, Some(Language::Ara)),
            (Script::Arabic, Some(Language::Pes)),
        ];
        assert_eq!(SEGMENTERS.len(), expected.len());
        for key in expected {
            assert!(SEGMENTERS.contains_key(&key), "{key:?}");
        }
        assert!(!SEGMENTERS.keys().any(|key| key.0 == Script::Cyrillic));
    }

    #[test]
    fn markup_delimiters_do_not_hide_ordinary_words_or_split_structured_tokens() {
        let input = "<a href=\"https://Example.com/A?q=100ms\">search &amp; ordinary</a>";
        let spans = segment(input, AnalysisMode::Structured).unwrap();
        let words: Vec<_> = spans
            .iter()
            .filter(|s| s.token_type.is_language())
            .map(|s| &input[s.start..s.end])
            .collect();
        assert!(words.contains(&"search"));
        assert!(words.contains(&"ordinary"));
        assert!(spans.iter().any(|s| s.token_type
            == TokenType::Structured(crate::structured_tokens::StructuredTokenKind::Url)
            && &input[s.start..s.end] == "https://Example.com/A?q=100ms"));
        assert_eq!(
            spans
                .iter()
                .map(|s| &input[s.start..s.end])
                .collect::<String>(),
            input
        );
    }

    #[test]
    fn parser_prefix_classification_keeps_external_dictionaries_off_blank() {
        let input = "alpha, beta\t\u{1} CAFÉ";
        let spans = segment(input, AnalysisMode::Structured).unwrap();
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
            let spans = segment(&text, AnalysisMode::Structured).unwrap();
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
        let input = " ".repeat(4096);
        let spans = segment(&input, AnalysisMode::Structured).unwrap();
        assert_eq!(spans.len(), 4096);
        assert!(
            spans
                .iter()
                .all(|span| span.end - span.start == 1 && span.token_type == TokenType::Blank)
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
        assert!(segment("", AnalysisMode::Structured).unwrap().is_empty());
        assert_eq!(
            segment("a\0b", AnalysisMode::Structured).unwrap_err().kind,
            ErrorKind::InvalidInput
        );
        assert_eq!(
            segment(&"a".repeat(MAX_INPUT_BYTES + 1), AnalysisMode::Structured)
                .unwrap_err()
                .kind,
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

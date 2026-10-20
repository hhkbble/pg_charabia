use std::borrow::Cow;
use std::fmt;

use charabia::normalizer::{ClassifierOption, NormalizerOption};
use charabia::{Normalize, SeparatorKind, Token, TokenKind, TokenizerBuilder};
use serde::de::{self, Deserialize, Deserializer, SeqAccess, Visitor};

use crate::profile::NormalizationRoute;

pub const MAX_INPUT_BYTES: usize = 262_144;
pub const MAX_STOP_WORDS_JSON_BYTES: usize = 1_048_576;
pub const MAX_STOP_WORDS: usize = 65_536;

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
    fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
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
    pub route: NormalizationRoute,
}

#[derive(Debug, Clone)]
pub struct Options {
    pub lossy: bool,
    pub stop_words: Option<fst::Set<Vec<u8>>>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            lossy: true,
            stop_words: None,
        }
    }
}

impl Options {
    pub fn from_json(lossy: bool, json: Option<&str>) -> Result<Self, AnalysisError> {
        let Some(json) = json else {
            return Ok(Self {
                lossy,
                stop_words: None,
            });
        };
        if json.len() > MAX_STOP_WORDS_JSON_BYTES {
            return Err(AnalysisError::new(
                ErrorKind::Limit,
                "StopWords JSON exceeds 1048576 bytes",
            ));
        }
        let StopWords(mut words) = serde_json::from_str(json).map_err(|error| {
            let kind = if error.to_string().starts_with("limit:") {
                ErrorKind::Limit
            } else {
                ErrorKind::InvalidInput
            };
            AnalysisError::new(kind, format!("invalid StopWords JSON: {error}"))
        })?;
        words.sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
        words.dedup();
        let stop_words = if words.is_empty() {
            None
        } else {
            Some(fst::Set::from_iter(words).map_err(|error| {
                AnalysisError::new(
                    ErrorKind::Internal,
                    format!("cannot build stopword FST: {error}"),
                )
            })?)
        };
        Ok(Self { lossy, stop_words })
    }

    fn normalizer_options(&self) -> Result<NormalizerOption<'_>, AnalysisError> {
        let stop_words = self
            .stop_words
            .as_ref()
            .map(|words| {
                fst::Set::new(words.as_fst().as_bytes()).map_err(|error| {
                    AnalysisError::new(
                        ErrorKind::Internal,
                        format!("invalid stopword FST: {error}"),
                    )
                })
            })
            .transpose()?;
        Ok(NormalizerOption {
            create_char_map: false,
            lossy: self.lossy,
            classifier: ClassifierOption {
                stop_words,
                separators: None,
            },
        })
    }
}

struct StopWords(Vec<String>);

impl<'de> Deserialize<'de> for StopWords {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct WordsVisitor;
        impl<'de> Visitor<'de> for WordsVisitor {
            type Value = StopWords;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("an array of stopword strings")
            }

            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut sequence: A,
            ) -> Result<Self::Value, A::Error> {
                let mut words = Vec::new();
                while let Some(word) = sequence.next_element::<String>()? {
                    if words.len() == MAX_STOP_WORDS {
                        return Err(de::Error::custom("limit: StopWords exceeds 65536 elements"));
                    }
                    if word.len() > MAX_INPUT_BYTES {
                        return Err(de::Error::custom(
                            "limit: StopWords item exceeds 262144 bytes",
                        ));
                    }
                    if word.contains('\0') {
                        return Err(de::Error::custom("StopWords item contains NUL"));
                    }
                    words.push(word);
                }
                Ok(StopWords(words))
            }
        }
        deserializer.deserialize_seq(WordsVisitor)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LexizeOutcome {
    Term(String),
    Stop,
    Ignore,
}

pub fn segment(input: &str) -> Result<Vec<Span>, AnalysisError> {
    check_input(input)?;
    let mut builder = TokenizerBuilder::default();
    let tokenizer = builder.build();
    let mut spans = Vec::new();
    let mut previous_end = 0;
    for token in tokenizer.segment(input) {
        check_span(input, token.byte_start, token.byte_end, previous_end)?;
        spans.push(Span {
            start: token.byte_start,
            end: token.byte_end,
            route: NormalizationRoute::from_metadata(token.script, token.language),
        });
        previous_end = token.byte_end;
    }
    if previous_end != input.len() {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "Charabia spans do not cover input",
        ));
    }
    Ok(spans)
}

pub fn normalize(
    raw: &str,
    route: NormalizationRoute,
    options: &Options,
) -> Result<LexizeOutcome, AnalysisError> {
    check_input(raw)?;
    let (script, language) = route.metadata();
    // Keep Unknown so Charabia's classifier runs before the lossy normalizers.
    let token = Token {
        lemma: Cow::Borrowed(raw),
        script,
        language,
        ..Token::default()
    }
    .normalize(&options.normalizer_options()?);
    outcome(token.kind, token.lemma())
}

fn outcome(kind: TokenKind, lemma: &str) -> Result<LexizeOutcome, AnalysisError> {
    match kind {
        TokenKind::Word if !lemma.is_empty() => Ok(LexizeOutcome::Term(lemma.to_owned())),
        TokenKind::StopWord => Ok(LexizeOutcome::Stop),
        TokenKind::Separator(_) | TokenKind::Word => Ok(LexizeOutcome::Ignore),
        TokenKind::Unknown => Err(AnalysisError::new(
            ErrorKind::Internal,
            "Charabia returned Unknown token kind",
        )),
    }
}

fn check_input(input: &str) -> Result<(), AnalysisError> {
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

fn check_span(
    input: &str,
    start: usize,
    end: usize,
    previous_end: usize,
) -> Result<(), AnalysisError> {
    if start != previous_end || start >= end || input.get(start..end).is_none() {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "Charabia returned an invalid UTF-8 span",
        ));
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnalyzedToken {
    pub raw: String,
    pub lemma: String,
    pub kind: String,
    pub script: String,
    pub language: Option<String>,
    pub byte_start: usize,
    pub byte_end: usize,
    pub char_start: usize,
    pub char_end: usize,
    pub route: NormalizationRoute,
}

pub fn analyze(input: &str) -> Result<Vec<AnalyzedToken>, AnalysisError> {
    check_input(input)?;
    let mut builder = TokenizerBuilder::default();
    builder.lossy_normalization(true).create_char_map(false);
    let tokenizer = builder.build();
    let mut result = Vec::new();
    let mut previous_end = 0;
    for token in tokenizer.tokenize(input) {
        check_span(input, token.byte_start, token.byte_end, previous_end)?;
        let kind = match token.kind {
            TokenKind::Word => "word",
            TokenKind::StopWord => "stopword",
            TokenKind::Separator(SeparatorKind::Soft) => "separator_soft",
            TokenKind::Separator(SeparatorKind::Hard) => "separator_hard",
            TokenKind::Unknown => {
                return Err(AnalysisError::new(
                    ErrorKind::Internal,
                    "Charabia returned Unknown token kind",
                ));
            }
        };
        result.push(AnalyzedToken {
            raw: input[token.byte_start..token.byte_end].to_owned(),
            lemma: token.lemma().to_owned(),
            kind: kind.to_owned(),
            script: format!("{:?}", token.script),
            language: token.language.map(|language| language.code().to_owned()),
            byte_start: token.byte_start,
            byte_end: token.byte_end,
            char_start: token.char_start,
            char_end: token.char_end,
            route: NormalizationRoute::from_metadata(token.script, token.language),
        });
        previous_end = token.byte_end;
    }
    if previous_end != input.len() {
        return Err(AnalysisError::new(
            ErrorKind::Internal,
            "Charabia tokens do not cover input",
        ));
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use charabia::{Language, Script};

    use super::*;

    #[test]
    fn word_stop_ignore_and_empty_lemma_are_distinct() {
        let options = Options::from_json(true, Some(r#"["the"]"#)).unwrap();
        let route = NormalizationRoute::Latin;
        assert_eq!(
            normalize("The", route, &options).unwrap(),
            LexizeOutcome::Term("the".into())
        );
        assert_eq!(
            normalize("the", route, &options).unwrap(),
            LexizeOutcome::Stop
        );
        assert_eq!(
            normalize(" ", route, &options).unwrap(),
            LexizeOutcome::Ignore
        );
        assert_eq!(
            normalize("\u{301}", route, &options).unwrap(),
            LexizeOutcome::Ignore
        );
        assert_eq!(
            normalize("Thé", route, &options).unwrap(),
            LexizeOutcome::Term("the".into())
        );
        assert_eq!(
            normalize("", route, &Options::default()).unwrap(),
            LexizeOutcome::Ignore
        );
        let empty_stopword = Options::from_json(true, Some(r#"[""]"#)).unwrap();
        assert_eq!(
            normalize("", route, &empty_stopword).unwrap(),
            LexizeOutcome::Stop
        );
        let lossless = Options::from_json(false, None).unwrap();
        assert_eq!(
            normalize("Thé", route, &lossless).unwrap(),
            LexizeOutcome::Term("The\u{301}".into())
        );
    }

    #[test]
    fn input_and_utf8_span_checks_reject_invalid_ranges() {
        assert!(segment("").unwrap().is_empty());
        assert_eq!(segment("a\0b").unwrap_err().kind, ErrorKind::InvalidInput);
        let oversized = "a".repeat(MAX_INPUT_BYTES + 1);
        assert_eq!(segment(&oversized).unwrap_err().kind, ErrorKind::Limit);
        assert_eq!(
            normalize(&oversized, NormalizationRoute::Latin, &Options::default())
                .unwrap_err()
                .kind,
            ErrorKind::Limit
        );
        assert_eq!(analyze(&oversized).unwrap_err().kind, ErrorKind::Limit);
        assert!(check_input(&"a".repeat(MAX_INPUT_BYTES)).is_ok());
        assert!(check_input(&"中".repeat(MAX_INPUT_BYTES / 3)).is_ok());
        assert_eq!(
            check_input(&"中".repeat(MAX_INPUT_BYTES / 3 + 1))
                .unwrap_err()
                .kind,
            ErrorKind::Limit
        );
        assert!(check_span("中a", 0, 3, 0).is_ok());
        for (start, end, previous) in [(1, 3, 0), (0, 1, 0), (0, 0, 0), (0, 5, 0)] {
            assert_eq!(
                check_span("中a", start, end, previous).unwrap_err().kind,
                ErrorKind::Internal
            );
        }
    }

    // Inputs and counts are the preserved 2026-10-20 Charabia source experiment.
    // Its direct Token::normalize result is independent of the PostgreSQL adapter.
    #[test]
    fn all_metadata_normalization_routes_match_charabia_89856_cases() {
        let scripts = [
            Script::Arabic,
            Script::Armenian,
            Script::Bengali,
            Script::Cyrillic,
            Script::Devanagari,
            Script::Ethiopic,
            Script::Georgian,
            Script::Greek,
            Script::Gujarati,
            Script::Gurmukhi,
            Script::Hangul,
            Script::Hebrew,
            Script::Kannada,
            Script::Khmer,
            Script::Latin,
            Script::Malayalam,
            Script::Myanmar,
            Script::Oriya,
            Script::Sinhala,
            Script::Tamil,
            Script::Telugu,
            Script::Thai,
            Script::Cj,
            Script::Other,
        ];
        let languages = [
            None,
            Some(Language::Zho),
            Some(Language::Epo),
            Some(Language::Eng),
            Some(Language::Rus),
            Some(Language::Cmn),
            Some(Language::Spa),
            Some(Language::Por),
            Some(Language::Ita),
            Some(Language::Ben),
            Some(Language::Fra),
            Some(Language::Deu),
            Some(Language::Ukr),
            Some(Language::Kat),
            Some(Language::Ara),
            Some(Language::Hin),
            Some(Language::Jpn),
            Some(Language::Heb),
            Some(Language::Yid),
            Some(Language::Pol),
            Some(Language::Amh),
            Some(Language::Jav),
            Some(Language::Kor),
            Some(Language::Nob),
            Some(Language::Dan),
            Some(Language::Swe),
            Some(Language::Fin),
            Some(Language::Tur),
            Some(Language::Nld),
            Some(Language::Hun),
            Some(Language::Ces),
            Some(Language::Ell),
            Some(Language::Bul),
            Some(Language::Bel),
            Some(Language::Mar),
            Some(Language::Kan),
            Some(Language::Ron),
            Some(Language::Slv),
            Some(Language::Hrv),
            Some(Language::Srp),
            Some(Language::Mkd),
            Some(Language::Lit),
            Some(Language::Lav),
            Some(Language::Est),
            Some(Language::Tam),
            Some(Language::Vie),
            Some(Language::Urd),
            Some(Language::Tha),
            Some(Language::Guj),
            Some(Language::Uzb),
            Some(Language::Pan),
            Some(Language::Aze),
            Some(Language::Ind),
            Some(Language::Tel),
            Some(Language::Pes),
            Some(Language::Mal),
            Some(Language::Ori),
            Some(Language::Mya),
            Some(Language::Nep),
            Some(Language::Sin),
            Some(Language::Khm),
            Some(Language::Tuk),
            Some(Language::Aka),
            Some(Language::Zul),
            Some(Language::Sna),
            Some(Language::Afr),
            Some(Language::Lat),
            Some(Language::Slk),
            Some(Language::Cat),
            Some(Language::Tgl),
            Some(Language::Hye),
            Some(Language::Cym),
        ];
        let samples = [
            "Å Ä Ö A\u{30a} éclair",
            "Đại Việt ı œ Æ",
            "ΣΟΣ ς",
            "أَب يى ۱۲۳ ﷼",
            "尊嚴 臺灣台灣",
            "カタカナ ガギ",
            "한국어",
            "foo\u{0001}bar",
            "\u{0001}",
            "\u{0301}",
            "the The THE",
            "。！. ",
            "👩\u{200d}💻",
        ];
        let mut count = 0;
        let mut routes = std::collections::HashSet::new();
        for script in scripts {
            for language in languages {
                for sample in samples {
                    for lossy in [false, true] {
                        for stop in [false, true] {
                            let options =
                                Options::from_json(lossy, stop.then_some(r#"["the"]"#)).unwrap();
                            let normalizer = options.normalizer_options().unwrap();
                            let expected = Token {
                                lemma: Cow::Borrowed(sample),
                                script,
                                language,
                                ..Token::default()
                            }
                            .normalize(&normalizer);
                            let route = NormalizationRoute::from_metadata(script, language);
                            routes.insert(route);
                            let (canonical_script, canonical_language) = route.metadata();
                            let canonical = Token {
                                lemma: Cow::Borrowed(sample),
                                script: canonical_script,
                                language: canonical_language,
                                ..Token::default()
                            }
                            .normalize(&normalizer);
                            assert_eq!(
                                (expected.lemma(), expected.kind),
                                (canonical.lemma(), canonical.kind),
                                "{script:?}/{language:?}/{sample:?}/{lossy}/{stop}"
                            );
                            assert_eq!(
                                normalize(sample, route, &options).unwrap(),
                                outcome(expected.kind, expected.lemma()).unwrap()
                            );
                            count += 1;
                        }
                    }
                }
            }
        }
        assert_eq!(count, 89_856);
        assert_eq!(routes.len(), 18);
    }

    #[test]
    fn direct_pipeline_matches_route_normalization_93_segments() {
        let fixtures = [
            (None, "繁體中文和简体中文：臺灣台灣，後端后端。"),
            (None, "関西国際空港限定トートバッグ 日本語の検索"),
            (None, "한국어의형태해석을실시할수있습니다"),
            (Some(Language::Swe), "Å Ä Ö å ä ö éclair"),
            (Some(Language::Vie), "Đại Việt Ð đ ð"),
            (Some(Language::Tur), "İstanbul çalışma ışık"),
            (Some(Language::Pes), "يك یك ۱۲۳ ﷼"),
            (Some(Language::Jpn), "漢字 カタカナ ガギグゲゴ"),
            (Some(Language::Cmn), "尊嚴 生而自由 臺灣台灣"),
            (Some(Language::Eng), "running runs runner"),
        ];
        let mut count = 0;
        for (language, input) in fixtures {
            let allow_list = language.map(|language| [language]);
            let mut builder = TokenizerBuilder::default();
            builder.lossy_normalization(true).create_char_map(false);
            if let Some(allow_list) = &allow_list {
                builder.allow_list(allow_list);
            }
            let tokenizer = builder.build();
            let native: Vec<_> = tokenizer.tokenize(input).collect();
            let segmented: Vec<_> = tokenizer.segment(input).collect();
            assert_eq!(native.len(), segmented.len());
            let mut previous_end = 0;
            for (expected, raw_token) in native.iter().zip(&segmented) {
                check_span(
                    input,
                    raw_token.byte_start,
                    raw_token.byte_end,
                    previous_end,
                )
                .unwrap();
                let raw = &input[raw_token.byte_start..raw_token.byte_end];
                assert_eq!(expected.byte_start, raw_token.byte_start);
                assert_eq!(expected.byte_end, raw_token.byte_end);
                assert_eq!(expected.char_start, raw_token.char_start);
                assert_eq!(expected.char_end, raw_token.char_end);
                assert_eq!(
                    normalize(
                        raw,
                        NormalizationRoute::from_metadata(raw_token.script, raw_token.language),
                        &Options::default(),
                    )
                    .unwrap(),
                    outcome(expected.kind, expected.lemma()).unwrap(),
                    "{raw:?}/{:?}/{:?}",
                    raw_token.script,
                    raw_token.language
                );
                previous_end = raw_token.byte_end;
                count += 1;
            }
            assert_eq!(previous_end, input.len());
        }
        assert_eq!(count, 93);
    }

    #[test]
    fn stopword_json_preserves_exact_decoded_bytes_and_ordering() {
        let options = Options::from_json(
            false,
            Some(r#"["the","THE",""," 中 ","the","\\","\"","\u00e9"]"#),
        )
        .unwrap();
        assert!(!options.lossy);
        let words = options.stop_words.unwrap();
        assert_eq!(words.len(), 7);
        for word in ["the", "THE", "", " 中 ", "\\", "\"", "é"] {
            assert!(words.contains(word));
        }
        assert!(!words.contains("The"));
        assert!(!words.contains("中"));
        assert!(
            Options::from_json(true, Some("[]"))
                .unwrap()
                .stop_words
                .is_none()
        );
        assert!(Options::from_json(true, None).unwrap().lossy);
        let empty = Options::from_json(true, Some(r#"[""]"#)).unwrap();
        assert_eq!(
            normalize("\u{301}", NormalizationRoute::Latin, &empty).unwrap(),
            LexizeOutcome::Ignore
        );
        let punctuation = Options::from_json(true, Some(r#"["."]"#)).unwrap();
        assert_eq!(
            normalize(".", NormalizationRoute::Latin, &punctuation).unwrap(),
            LexizeOutcome::Stop
        );
    }

    #[test]
    fn stopword_json_rejects_invalid_shape_nul_and_trailing_data() {
        for json in [
            "null",
            "{}",
            r#""limit:""#,
            "[null]",
            "[true]",
            "[1]",
            "[[\"a\"]]",
            "[{\"a\":1}]",
            r#"["\u0000"]"#,
            r#"["\ud800"]"#,
            "[\"a\"] false",
            "[",
        ] {
            assert_eq!(
                Options::from_json(true, Some(json)).unwrap_err().kind,
                ErrorKind::InvalidInput,
                "{json}"
            );
        }
    }

    #[test]
    fn stopword_json_limits_apply_before_deduplication() {
        let max_item = format!("[\"{}\"]", "a".repeat(MAX_INPUT_BYTES));
        assert!(
            Options::from_json(true, Some(&max_item))
                .unwrap()
                .stop_words
                .unwrap()
                .contains("a".repeat(MAX_INPUT_BYTES))
        );
        let large_item = format!("[\"{}\"]", "a".repeat(MAX_INPUT_BYTES + 1));
        assert_eq!(
            Options::from_json(true, Some(&large_item))
                .unwrap_err()
                .kind,
            ErrorKind::Limit
        );
        let max_count = format!("[{}]", vec!["\"\""; MAX_STOP_WORDS].join(","));
        assert_eq!(
            Options::from_json(true, Some(&max_count))
                .unwrap()
                .stop_words
                .unwrap()
                .len(),
            1
        );
        let large_count = format!("[{}]", vec!["\"\""; MAX_STOP_WORDS + 1].join(","));
        assert_eq!(
            Options::from_json(true, Some(&large_count))
                .unwrap_err()
                .kind,
            ErrorKind::Limit
        );
        let max_json = format!("{}[]", " ".repeat(MAX_STOP_WORDS_JSON_BYTES - 2));
        assert!(Options::from_json(true, Some(&max_json)).is_ok());
        let large_json = format!(" {max_json}");
        assert_eq!(
            Options::from_json(true, Some(&large_json))
                .unwrap_err()
                .kind,
            ErrorKind::Limit
        );
    }

    #[test]
    fn immutable_observed_auto_profile_fixtures_match_all_diagnostic_fields() {
        let fixtures: serde_json::Value = serde_json::from_str(include_str!(
            "../tests/fixtures/analysis/observed-auto-v1.json"
        ))
        .unwrap();
        for fixture in fixtures["fixtures"].as_array().unwrap() {
            let input = fixture["input"].as_str().unwrap();
            let expected = fixture["tokens"].as_array().unwrap();
            let tokens = analyze(input).unwrap();
            let spans = segment(input).unwrap();
            assert_eq!(tokens.len(), expected.len(), "{input:?}");
            assert_eq!(spans.len(), expected.len(), "{input:?}");
            for ((token, span), expected) in tokens.iter().zip(&spans).zip(expected) {
                assert_eq!(
                    &serde_json::json!({
                        "raw": token.raw,
                        "lemma": token.lemma,
                        "kind": token.kind,
                        "script": token.script,
                        "language": token.language,
                        "byte_start": token.byte_start,
                        "byte_end": token.byte_end,
                        "char_start": token.char_start,
                        "char_end": token.char_end,
                        "route": token.route.alias(),
                    }),
                    expected,
                    "{input:?}"
                );
                assert_eq!(span.start, token.byte_start);
                assert_eq!(span.end, token.byte_end);
                assert_eq!(span.route, token.route);
                assert_eq!(input[..token.byte_start].chars().count(), token.char_start);
                assert_eq!(input[..token.byte_end].chars().count(), token.char_end);
            }
            assert_eq!(
                tokens
                    .iter()
                    .map(|token| token.raw.as_str())
                    .collect::<String>(),
                input
            );
        }
    }
}

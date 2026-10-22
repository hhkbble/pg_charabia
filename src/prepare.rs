//! Shared host/target preparation. Keep this free of PostgreSQL and resource state.
use crate::language_data::LanguageId;
use charabia::normalizer::{
    Classifier, ClassifierOption, CompatibilityDecompositionNormalizer, ControlCharNormalizer,
    Normalizer, NormalizerOption,
};
use charabia::{Normalize, Token, TokenKind};
use std::{borrow::Cow, fmt};
use unicode_normalization::UnicodeNormalization;
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Prepared {
    pub kind: TokenKind,
    pub key: String,
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

pub fn normalizer(lossy: bool) -> NormalizerOption<'static> {
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

pub fn finalize(key: &str, route: LanguageId) -> String {
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

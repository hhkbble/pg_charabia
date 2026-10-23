//! Bounded, terminal symbol terms. Structural spelling is separate from English analysis.
use charabia::segmenter::{LatinSegmenter, Segmenter};
use iri_string::types::UriStr;

use super::{SymbolKind, recognition};
use crate::analysis::{self, AnalysisError, ErrorKind};
use crate::routes::LanguageId;

pub const MAX_UNITS: usize = 32;
pub const MAX_TERMS: usize = 66;
pub const MAX_OUTPUT_BYTES: usize = 16 * 1024;

#[derive(Debug)]
pub struct Options {
    kind: SymbolKind,
    english: analysis::Options,
}

impl Options {
    pub fn new(
        kind: SymbolKind,
        discard: bool,
        stem: bool,
        normalize: bool,
    ) -> Result<Self, AnalysisError> {
        Ok(Self {
            kind,
            english: analysis::Options::new(
                LanguageId::ENG,
                discard,
                stem,
                normalize,
                false,
                false,
            )?,
        })
    }

    pub fn kind(&self) -> SymbolKind {
        self.kind
    }

    #[cfg(any(test, feature = "test-hooks"))]
    pub fn referenced_fst_bytes(&self) -> usize {
        self.english.referenced_fst_bytes()
    }
}

/// Full terms are separate OR variants; all components form one AND variant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SymbolTerms {
    full: Vec<String>,
    components: Vec<String>,
}

impl SymbolTerms {
    #[cfg(test)]
    pub fn full(&self) -> &[String] {
        &self.full
    }

    #[cfg(test)]
    pub fn components(&self) -> &[String] {
        &self.components
    }

    pub fn len(&self) -> usize {
        self.full.len() + self.components.len()
    }

    pub fn into_groups(self) -> (Vec<String>, Vec<String>) {
        (self.full, self.components)
    }
}

fn limit(message: &'static str) -> AnalysisError {
    AnalysisError::new(ErrorKind::Limit, message)
}

struct Builder<'a> {
    options: &'a Options,
    terms: SymbolTerms,
    units: usize,
    // Bounded by units. This cache belongs only to this token, not the backend.
    words: Vec<(String, Vec<String>)>,
}

impl<'a> Builder<'a> {
    fn new(options: &'a Options, full: Vec<String>) -> Self {
        Self {
            options,
            terms: SymbolTerms {
                full,
                components: Vec::new(),
            },
            units: 0,
            words: Vec::new(),
        }
    }

    fn unit(&mut self) -> Result<(), AnalysisError> {
        self.units = self
            .units
            .checked_add(1)
            .ok_or_else(|| limit("symbol expansion exceeds 32 units"))?;
        if self.units > MAX_UNITS {
            return Err(limit("symbol expansion exceeds 32 units"));
        }
        Ok(())
    }

    fn push(&mut self, term: String) {
        if !term.is_empty()
            && !self.terms.full.contains(&term)
            && !self.terms.components.contains(&term)
        {
            self.terms.components.push(term);
        }
    }

    fn literal(&mut self, raw: &str) -> Result<(), AnalysisError> {
        if raw.is_empty() || self.terms.full.iter().any(|full| full == raw) {
            return Ok(());
        }
        self.unit()?;
        self.push(raw.to_owned());
        Ok(())
    }

    fn word(&mut self, raw: &str) -> Result<(), AnalysisError> {
        // Count before stop processing or cache lookup, including repeated words.
        self.unit()?;
        if let Some((_, terms)) = self.words.iter().find(|(word, _)| word == raw) {
            let terms = terms.clone();
            for term in terms {
                self.push(term);
            }
            return Ok(());
        }
        let terms = analysis::english_component(raw, &self.options.english)?;
        for term in &terms {
            self.push(term.clone());
        }
        self.words.push((raw.to_owned(), terms));
        Ok(())
    }

    fn alphabetic(&mut self, raw: &str) -> Result<(), AnalysisError> {
        let mut words = LatinSegmenter.segment_str(raw);
        let Some(first) = words.next() else {
            return Ok(());
        };
        let Some(second) = words.next() else {
            return self.word(first);
        };
        if crate::resources::english_contains(&raw.to_ascii_lowercase()) {
            return self.word(raw);
        }
        self.word(first)?;
        self.word(second)?;
        for word in words {
            self.word(word)?;
        }
        Ok(())
    }

    /// One non-recursive scan; valid percent triplets do not become word leaves.
    fn leaves(&mut self, raw: &str) -> Result<(), AnalysisError> {
        let bytes = raw.as_bytes();
        let mut cursor = 0;
        while cursor < bytes.len() {
            if bytes[cursor] == b'%'
                && bytes.get(cursor + 1).is_some_and(u8::is_ascii_hexdigit)
                && bytes.get(cursor + 2).is_some_and(u8::is_ascii_hexdigit)
            {
                cursor += 3;
                continue;
            }
            let start = cursor;
            if bytes[cursor].is_ascii_alphabetic() {
                cursor += 1;
                while cursor < bytes.len() && bytes[cursor].is_ascii_alphabetic() {
                    cursor += 1;
                }
                self.alphabetic(&raw[start..cursor])?;
            } else if bytes[cursor].is_ascii_digit() {
                cursor += 1;
                while cursor < bytes.len() && bytes[cursor].is_ascii_digit() {
                    cursor += 1;
                }
                self.literal(&raw[start..cursor])?;
            } else {
                cursor += 1;
            }
        }
        Ok(())
    }

    fn piece(&mut self, raw: &str) -> Result<(), AnalysisError> {
        self.literal(raw)?;
        self.leaves(raw)
    }

    fn host(&mut self, host: &str, port: Option<&str>, is_ip: bool) -> Result<(), AnalysisError> {
        let canonical = host.to_ascii_lowercase();
        self.literal(&canonical)?;
        if let Some(port) = port {
            self.literal(&format!("{canonical}:{port}"))?;
            self.literal(port)?;
        }
        if !is_ip {
            let mut labels = host.trim_end_matches('.').split('.').peekable();
            while let Some(label) = labels.next() {
                if labels.peek().is_some() || !host.contains('.') {
                    self.literal(&label.to_ascii_lowercase())?;
                    // DNS identity folds case, but the raw spelling retains camel boundaries.
                    self.leaves(label)?;
                }
            }
        }
        Ok(())
    }

    fn path(&mut self, path: &str, include_whole: bool) -> Result<(), AnalysisError> {
        if include_whole {
            self.literal(path)?;
        }
        // The final segment already supplies the basename; do not emit it twice.
        for segment in path.split(['/', '\\']).filter(|part| !part.is_empty()) {
            self.piece(segment)?;
        }
        Ok(())
    }

    fn finish(self) -> Result<SymbolTerms, AnalysisError> {
        if self.terms.full.is_empty() || self.terms.full.len() > 2 || self.terms.len() > MAX_TERMS {
            return Err(limit("symbol terms exceed 66 entries"));
        }
        let bytes = self
            .terms
            .full
            .iter()
            .chain(&self.terms.components)
            .try_fold(0usize, |sum, term| sum.checked_add(term.len()));
        if bytes.is_none_or(|bytes| bytes > MAX_OUTPUT_BYTES) {
            return Err(limit("symbol terms exceed 16384 bytes"));
        }
        Ok(self.terms)
    }
}

fn canonical_uri_host(raw: &str) -> String {
    let mut result = String::with_capacity(raw.len());
    let mut index = 0;
    while index < raw.len() {
        if raw.as_bytes()[index] == b'%' {
            // UriStr validated the complete triplet. Its literal spelling stays opaque.
            result.push_str(&raw[index..index + 3]);
            index += 3;
        } else {
            result.push(raw.as_bytes()[index].to_ascii_lowercase() as char);
            index += 1;
        }
    }
    result
}

/// Canonicalize only scheme and host; retain every other byte of URI spelling.
fn canonical_uri(uri: &UriStr) -> String {
    let raw = uri.as_str();
    let mut full = raw.to_owned();
    let scheme_len = uri.scheme_str().len();
    full[..scheme_len].make_ascii_lowercase();
    if let Some(authority) = uri.authority_components() {
        let host = authority.host();
        let offset = host.as_ptr() as usize - raw.as_ptr() as usize;
        full.replace_range(offset..offset + host.len(), &canonical_uri_host(host));
    }
    full
}

pub fn analyze(raw: &str, options: &Options) -> Result<Option<SymbolTerms>, AnalysisError> {
    analysis::check_input(raw)?;
    let Some(parsed) = recognition::parse(options.kind(), raw) else {
        return Ok(None);
    };
    use recognition::{Numword, Parsed};
    let builder = match parsed {
        Parsed::Url { uri, recall } => {
            let mut full = vec![canonical_uri(uri)];
            if let Some(recall) = recall {
                let alias = canonical_uri(recall);
                if !full.contains(&alias) {
                    full.push(alias);
                }
            }
            let uri = recall.unwrap_or(uri);
            let mut builder = Builder::new(options, full);
            if let Some(authority) = uri.authority_components() {
                // Validation already checked authority syntax; only metadata is reused.
                if !authority.host().is_empty() {
                    let host = authority.host();
                    let is_ip = host.starts_with('[') || host.parse::<std::net::Ipv4Addr>().is_ok();
                    if recognition::host(host.strip_suffix('.').unwrap_or(host), true).is_some() {
                        builder.host(host, authority.port(), is_ip)?;
                    } else {
                        // URI reg-names need not be DNS names. Keep one opaque
                        // host component instead of inventing DNS labels or decoding.
                        let host = canonical_uri_host(host);
                        builder.literal(&host)?;
                        if let Some(port) = authority.port() {
                            builder.literal(&format!("{host}:{port}"))?;
                            builder.literal(port)?;
                        }
                    }
                }
            }
            builder.path(uri.path_str(), true)?;
            if let Some(query) = uri.query_str() {
                for pair in query.split('&') {
                    if let Some((key, value)) = pair.split_once('=') {
                        builder.piece(key)?;
                        builder.piece(value)?;
                    } else {
                        builder.piece(pair)?;
                    }
                }
            }
            if let Some(fragment) = uri.fragment_str() {
                builder.piece(fragment)?;
            }
            builder
        }
        Parsed::Host(host) => {
            let full = match host.port {
                Some(port) => format!("{}:{port}", host.host.to_ascii_lowercase()),
                None => host.host.to_ascii_lowercase(),
            };
            let mut builder = Builder::new(options, vec![full]);
            builder.host(host.host, host.port, host.is_ip)?;
            builder
        }
        Parsed::Email { local, domain } => {
            let mut builder = Builder::new(
                options,
                vec![format!("{local}@{}", domain.to_ascii_lowercase())],
            );
            builder.piece(local)?;
            builder.host(domain, None, false)?;
            builder
        }
        Parsed::File(path) => {
            let mut builder = Builder::new(options, vec![path.to_owned()]);
            builder.path(path, false)?;
            builder
        }
        Parsed::Numword(Numword::Hex(body)) => Builder::new(
            options,
            vec![
                format!("0x{}", body.to_ascii_lowercase()),
                body.to_ascii_lowercase(),
            ],
        ),
        Parsed::Numword(Numword::Literal) => Builder::new(options, vec![raw.to_owned()]),
        Parsed::Numword(Numword::Mixed) => {
            let mut full = vec![raw.to_owned()];
            let lower = raw.to_ascii_lowercase();
            if full[0] != lower {
                full.push(lower);
            }
            let mut builder = Builder::new(options, full);
            for chunk in raw.split(['_', '-']) {
                // Keep mixed connector chunks as literals, but not root-as-child.
                if chunk.bytes().any(|byte| byte.is_ascii_alphabetic())
                    && chunk.bytes().any(|byte| byte.is_ascii_digit())
                {
                    builder.literal(chunk)?;
                }
                builder.leaves(chunk)?;
            }
            builder
        }
    };
    builder.finish().map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn options(kind: SymbolKind) -> Options {
        Options::new(kind, true, true, true).unwrap()
    }

    fn terms(kind: SymbolKind, raw: &str) -> SymbolTerms {
        analyze(raw, &options(kind)).unwrap().unwrap()
    }

    #[test]
    fn units_expand_as_atomic_full_or_component_bundle() {
        let actual = terms(SymbolKind::Numword, "100ms");
        assert_eq!(actual.full(), ["100ms"]);
        assert_eq!(actual.components(), ["100", "ms"]);
        assert_eq!(actual.len(), 3);
        for raw in ["0x5fff", "0X5FFF", "5fff"] {
            let actual = terms(SymbolKind::Numword, raw);
            assert_eq!(actual.full(), ["0x5fff", "5fff"]);
            assert!(actual.components().is_empty());
        }
        let actual = terms(SymbolKind::Numword, "f5ff");
        assert_eq!(actual.full(), ["0xf5ff", "f5ff"]);
        assert!(actual.components().is_empty());
    }

    #[test]
    fn literal_numbers_and_versions_are_not_subdivided() {
        for raw in [
            "1234", "01", "-12.50", "1e3", "1e10", "1E10", "12e3", "1e1234", "v1.2.3",
        ] {
            let actual = terms(SymbolKind::Numword, raw);
            assert_eq!(actual.full(), [raw]);
            assert!(actual.components().is_empty(), "{raw}");
        }
    }

    #[test]
    fn uri_full_case_and_percent_spelling_remain_literal() {
        let raw = "HTTP://User:Pass@Example.COM:00080/A/../%7e?x=%41&x=A#Part";
        let actual = terms(SymbolKind::Url, raw);
        assert_eq!(
            actual.full(),
            ["http://User:Pass@example.com:00080/A/../%7e?x=%41&x=A#Part"]
        );
        assert!(actual.components().contains(&"example.com:00080".into()));
        assert!(actual.components().contains(&"%41".into()));
        for forbidden in [
            "http", "com", "User", "user", "Pass", "pass", "41", "7", "7e",
        ] {
            assert!(
                !actual.components().contains(&forbidden.into()),
                "{forbidden}"
            );
        }
    }

    #[test]
    fn uri_reg_names_do_not_inherit_standalone_dns_restrictions() {
        let actual = terms(SymbolKind::Url, "HTTPS://EXAMPLE_COM/A");
        assert_eq!(actual.full(), ["https://example_com/A"]);
        let actual = terms(SymbolKind::Url, "HTTPS://EXA%4Dple.COM/A");
        assert_eq!(actual.full(), ["https://exa%4Dple.com/A"]);
        assert!(actual.components().contains(&"exa%4Dple.com".into()));
        assert!(!actual.components().contains(&"4".into()));
    }

    #[test]
    fn structure_is_not_case_folded_by_the_english_options() {
        let actual = terms(SymbolKind::Email, "User+tag@Example.COM");
        assert_eq!(actual.full(), ["User+tag@example.com"]);
        assert!(actual.components().contains(&"User+tag".into()));
        assert!(actual.components().contains(&"user".into()));
        assert!(!actual.components().contains(&"com".into()));
        let actual = terms(SymbolKind::File, "/Src/RunningTests.rs");
        assert_eq!(actual.full(), ["/Src/RunningTests.rs"]);
        assert!(actual.components().contains(&"RunningTests.rs".into()));
        assert!(actual.components().contains(&"running".into()));
        assert!(actual.components().contains(&"run".into()));
    }

    #[test]
    fn dns_labels_retain_literal_identity_without_recursive_hex_expansion() {
        let actual = terms(SymbolKind::Host, "ABC123.Example.TEST");
        assert_eq!(actual.full(), ["abc123.example.test"]);
        assert!(actual.components().contains(&"abc123".into()));
        assert!(actual.components().contains(&"abc".into()));
        assert!(actual.components().contains(&"123".into()));
        assert!(!actual.components().contains(&"0xabc123".into()));
        assert!(!actual.components().contains(&"test".into()));
        let actual = terms(SymbolKind::Host, "the.example");
        assert!(actual.components().contains(&"the".into()));
        assert!(!crate::resources::english_contains("databaseapi"));
        for (kind, raw) in [
            (SymbolKind::Host, "DatabaseAPI.example"),
            (SymbolKind::Url, "https://DatabaseAPI.example/"),
            (SymbolKind::Email, "User@DatabaseAPI.example"),
        ] {
            let actual = terms(kind, raw);
            assert!(actual.components().contains(&"databaseapi".into()));
            assert!(actual.components().contains(&"database".into()));
            assert!(actual.components().contains(&"api".into()));
            assert!(!actual.components().contains(&"example".into()));
        }
    }

    #[test]
    fn url_root_dot_does_not_turn_the_final_dns_label_into_a_component() {
        let actual = terms(SymbolKind::Url, "https://Example.COM./Running");
        assert_eq!(actual.full(), ["https://example.com./Running"]);
        assert!(actual.components().contains(&"example.com.".into()));
        assert!(!actual.components().contains(&"com".into()));
    }

    #[test]
    fn stop_does_not_delete_structural_literals_or_full_forms() {
        let actual = terms(SymbolKind::Url, "https://example.com/the");
        assert_eq!(actual.full(), ["https://example.com/the"]);
        assert!(actual.components().contains(&"the".into()));
        let actual = terms(SymbolKind::Numword, "the_1");
        assert!(!actual.components().contains(&"the".into()));
        assert!(actual.components().contains(&"1".into()));
        let actual = analyze(
            "the_1",
            &Options::new(SymbolKind::Numword, false, false, false).unwrap(),
        )
        .unwrap()
        .unwrap();
        assert!(actual.components().contains(&"the".into()));
    }

    #[test]
    fn all_symbol_boolean_options_are_valid_and_resources_are_shared() {
        for kind in [
            SymbolKind::Url,
            SymbolKind::Host,
            SymbolKind::Email,
            SymbolKind::File,
            SymbolKind::Numword,
        ] {
            for flags in 0..8 {
                let options =
                    Options::new(kind, flags & 1 != 0, flags & 2 != 0, flags & 4 != 0).unwrap();
                assert_eq!(options.kind(), kind);
                assert_eq!(options.referenced_fst_bytes() > 0, flags & 1 != 0);
            }
        }
    }

    #[test]
    fn budget_is_checked_before_stop_and_duplicate_removal() {
        let options = options(SymbolKind::Numword);
        let mut builder = Builder::new(&options, vec!["root".into()]);
        for _ in 0..32 {
            builder.word("the").unwrap();
        }
        assert!(builder.terms.components.is_empty());
        assert_eq!(builder.words.len(), 1);
        assert_eq!(builder.word("the").unwrap_err().kind, ErrorKind::Limit);
        let mut builder = Builder::new(&options, vec!["root".into()]);
        for _ in 0..32 {
            builder.literal("same").unwrap();
        }
        assert_eq!(builder.terms.components(), ["same"]);
        assert_eq!(builder.literal("same").unwrap_err().kind, ErrorKind::Limit);
    }

    #[test]
    fn output_budget_and_full_collisions_have_independent_checks() {
        let options = options(SymbolKind::Numword);
        let mut builder = Builder::new(&options, vec!["root".into()]);
        builder.literal("root").unwrap();
        builder.literal("other").unwrap();
        assert_eq!(builder.finish().unwrap().components(), ["other"]);
        let builder = Builder::new(&options, vec!["x".repeat(MAX_OUTPUT_BYTES + 1)]);
        assert_eq!(builder.finish().unwrap_err().kind, ErrorKind::Limit);
        let mut builder = Builder::new(&options, vec!["root".into()]);
        builder.terms.components = (0..MAX_TERMS).map(|n| n.to_string()).collect();
        assert_eq!(builder.finish().unwrap_err().kind, ErrorKind::Limit);
    }

    #[test]
    fn invalid_kind_input_returns_null_fallback_without_new_detection() {
        for (kind, raw) in [
            (SymbolKind::Url, "https://example.com/%ZZ"),
            (SymbolKind::Email, "not-an-email"),
            (SymbolKind::Host, "https://example.com"),
            (SymbolKind::Numword, "ordinary"),
        ] {
            assert!(analyze(raw, &options(kind)).unwrap().is_none(), "{raw}");
        }
    }
}

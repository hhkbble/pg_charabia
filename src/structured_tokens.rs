//! Bounded ASCII structures with atomic PostgreSQL positions.
pub(crate) mod recognition;
mod terms;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum StructuredTokenKind {
    Url,
    Host,
    Email,
    File,
    Numword,
}

impl StructuredTokenKind {
    pub const ALL: [Self; 5] = [
        Self::Url,
        Self::Host,
        Self::Email,
        Self::File,
        Self::Numword,
    ];
    pub fn alias(self) -> &'static str {
        match self {
            Self::Url => "url",
            Self::Host => "host",
            Self::Email => "email",
            Self::File => "file",
            Self::Numword => "numword",
        }
    }
    pub fn from_alias(alias: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|kind| kind.alias() == alias)
    }
    pub fn id(self) -> i32 {
        Self::ALL
            .iter()
            .position(|kind| *kind == self)
            .expect("fixed symbol kind") as i32
            + 19
    }
    pub fn from_id(id: i32) -> Option<Self> {
        id.checked_sub(19)
            .and_then(|index| Self::ALL.get(index as usize))
            .copied()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Candidate {
    pub start: usize,
    pub end: usize,
    pub kind: StructuredTokenKind,
}

pub(crate) use terms::{Options, StructuredTokenTerms, analyze};

pub(crate) fn identity() -> serde_json::Value {
    serde_json::json!({
        "rule_revision":"ascii-atomic-full-or-component-bundle-1",
        "dictionary_count":5,
        "ordinary_ascii_delimiters":["<",">","="],
        "types":StructuredTokenKind::ALL.map(|kind| serde_json::json!({
            "id":kind.id(),"kind":kind.alias(),
            "defaults":{"discard":true,"stem":true,"normalize":true}
        })),
        "limits":{"units":32,"lexemes":66,"bytes":16384},
        "dependencies":{"linkify":"0.11.0","iri-string":"0.7.14"}
    })
}

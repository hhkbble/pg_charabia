use pgrx::prelude::*;
use pgrx::{AnyElement, Internal, JsonB, pg_sys};
use sha2::{Digest, Sha256};

pgrx::pg_module_magic!();
mod analysis;
#[cfg(test)]
mod benchmark_tests;
mod headline;
mod language;
mod language_data;
mod linguistic_dictionary;
mod parser;
mod pg_ffi;
mod prepare;
mod resources;
mod routes;
mod stopwords;
mod structured_dictionary;
mod structured_tokens;
#[cfg(feature = "test-hooks")]
mod test_hooks;

const ANALYSIS_REVISION: &str = "route16-structured-tokens-1";

#[pg_extern(sql = false)]
fn analysis_info() -> JsonB {
    let fingerprint = format!(
        "{ANALYSIS_REVISION}:{}:{}:{}",
        env!("CARGO_PKG_VERSION"),
        env!("PG_CHARABIA_LOCK_SHA256"),
        env!("PG_CHARABIA_RESOURCES_SHA256")
    );
    let languages = routes::LanguageId::ALL.map(|language| serde_json::json!({
        "language":language.alias(),"script":format!("{:?}",language.script()),
        "has_stopwords":language.has_stopwords(),"stemmer":language.algorithm().map(|a|format!("{a:?}")),
        "english_eligible":language.english_eligible(),
        "defaults":{"discard":language.has_stopwords(),"stem":language.default_stem(language.default_english(false)),"english":language.default_english(false),"normalize":true,"filter":false}
    }));
    JsonB(serde_json::json!({
        "extension":env!("CARGO_PKG_VERSION"),"pgrx":"0.19.3","charabia":"0.10.0","target":env!("PG_CHARABIA_TARGET"),
        "cargo_lock_sha256":env!("PG_CHARABIA_LOCK_SHA256"),"resource_manifest_sha256":env!("PG_CHARABIA_RESOURCES_SHA256"),
        "analysis_revision":ANALYSIS_REVISION,"resource_fingerprint":format!("{:x}",Sha256::digest(fingerprint.as_bytes())),
        "language_count":routes::LanguageId::ALL.len(),"languages":languages.as_slice(),
        "structured_tokens":structured_tokens::identity(),
        "resources":serde_json::from_str::<serde_json::Value>(include_str!("../resources/manifest.json")).expect("fixed resource manifest")
    }))
}

#[pg_extern(sql = false)]
fn query_any(text: AnyElement, configuration: pg_sys::Oid) -> Internal {
    // SAFETY: The explicit SQL signature supplies a request-owned text Datum.
    let input = unsafe { pg_ffi::text_input(text) };
    pg_ffi::checked(analysis::check_input(input));
    pg_ffi::plain_query_any(configuration, text.datum())
}

#[pg_extern(sql = false)]
#[allow(
    clippy::type_complexity,
    reason = "Explicit SQL columns at the pgrx boundary"
)]
fn analyze(
    text: AnyElement,
) -> TableIterator<
    'static,
    (
        name!(raw, String),
        name!(kind, String),
        name!(script, String),
        name!(charabia_language, Option<String>),
        name!(language, Option<String>),
        name!(language_source, String),
        name!(byte_start, i64),
        name!(byte_end, i64),
        name!(char_start, i64),
        name!(char_end, i64),
        name!(token_type, String),
    ),
> {
    // SAFETY: The explicit text Datum and detoasted bytes live for this callback.
    let input = unsafe { pg_ffi::text_input(text) };
    let spans = pg_ffi::checked(analysis::segment(input));
    let mut rows = Vec::with_capacity(spans.len());
    for span in spans {
        pgrx::check_for_interrupts!();
        let kind = match span.token_type {
            routes::TokenType::Language(_) => "word",
            routes::TokenType::Structured(kind) => kind.alias(),
            routes::TokenType::Blank => "blank",
            routes::TokenType::Display => "display_only",
        };
        rows.push((
            input[span.start..span.end].to_owned(),
            kind.to_owned(),
            format!("{:?}", span.script),
            span.charabia_language.map(|l| l.code().to_owned()),
            span.token_type
                .is_language()
                .then(|| span.route.alias().to_owned()),
            span.language_source.as_str().to_owned(),
            span.start as i64,
            span.end as i64,
            span.char_start as i64,
            span.char_end as i64,
            span.token_type.alias().to_owned(),
        ));
    }
    TableIterator::new(rows)
}

pgrx::extension_sql_file!("../sql/pg_charabia.sql", name = "fts");
#[cfg(feature = "test-hooks")]
pgrx::extension_sql!(
    r#"
CREATE FUNCTION charabia.test_set_fault(text,text) RETURNS void
AS 'MODULE_PATHNAME','test_set_fault_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.test_stats() RETURNS jsonb
AS 'MODULE_PATHNAME','test_stats_wrapper' LANGUAGE c;
"#,
    name = "test_hooks",
    requires = ["fts"]
);

use pgrx::prelude::*;
use pgrx::{AnyElement, JsonB};

pgrx::pg_module_magic!();

mod analysis;
#[cfg(test)]
mod benchmark_tests;
mod dictionary;
mod parser;
mod pg_ffi;
mod profile;
#[cfg(feature = "test-hooks")]
mod test_hooks;

#[pg_extern(sql = false)]
#[allow(
    clippy::type_complexity,
    reason = "Keep the SQL record's named columns explicit at the pgrx boundary"
)]
fn analyze_v1(
    text: AnyElement,
) -> TableIterator<
    'static,
    (
        name!(raw, String),
        name!(lemma, String),
        name!(kind, String),
        name!(script, String),
        name!(language, Option<String>),
        name!(byte_start, i64),
        name!(byte_end, i64),
        name!(char_start, i64),
        name!(char_end, i64),
        name!(route, String),
    ),
> {
    // SAFETY: The explicit SQL definition accepts a live text Datum. Decoding
    // here lets the encoding and byte limit checks precede Rust str conversion.
    let text = unsafe { pg_ffi::text_input(text) };
    let rows = pg_ffi::checked(analysis::analyze(text));
    TableIterator::new(rows.into_iter().map(|row| {
        (
            row.raw,
            row.lemma,
            row.kind,
            row.script,
            row.language,
            row.byte_start as i64,
            row.byte_end as i64,
            row.char_start as i64,
            row.char_end as i64,
            row.route.alias().to_owned(),
        )
    }))
}

#[pg_extern(sql = false)]
fn analysis_info_v1() -> JsonB {
    JsonB(serde_json::json!({
        "extension": env!("CARGO_PKG_VERSION"), "charabia": "0.10.0", "pgrx": "0.19.3",
        "profile": "auto_v1", "target": env!("PG_CHARABIA_TARGET"),
        "cargo_lock_sha256": env!("PG_CHARABIA_LOCK_SHA256"),
        "resource_manifest_sha256": env!("PG_CHARABIA_RESOURCES_SHA256"),
        "test_hooks": cfg!(feature = "test-hooks"),
        "resources": serde_json::from_str::<serde_json::Value>(include_str!("../resources/manifest.json")).expect("validated resource manifest"),
    }))
}

pgrx::extension_sql_file!("../sql/fts_v1.sql", name = "fts_v1");

#[cfg(feature = "test-hooks")]
pgrx::extension_sql!(
    r#"
CREATE FUNCTION charabia.test_set_fault_v1(text,text) RETURNS void
AS 'MODULE_PATHNAME','test_set_fault_v1_wrapper' LANGUAGE c STRICT;
CREATE FUNCTION charabia.test_stats_v1() RETURNS jsonb
AS 'MODULE_PATHNAME','test_stats_v1_wrapper' LANGUAGE c;
"#,
    name = "test_hooks",
    requires = ["fts_v1"]
);

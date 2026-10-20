use pgrx::prelude::*;
use pgrx::{AnyElement, Internal, JsonB, pg_sys};

pgrx::pg_module_magic!();
mod analysis;
#[cfg(test)]
mod benchmark_tests;
mod config;
mod dictionary;
mod headline;
mod language;
mod parser;
mod pg_ffi;
mod routes;
mod settings;
mod stopwords;
#[cfg(feature = "test-hooks")]
mod test_hooks;

#[pg_extern(sql = false)]
fn create_config(name: &str, options: JsonB) -> pg_sys::Oid {
    config::create(name, options.0)
}
#[pg_extern(sql = false)]
fn alter_config(configuration: pg_sys::Oid, options: JsonB) -> JsonB {
    JsonB(config::alter(configuration, options.0))
}
#[pg_extern(sql = false)]
fn drop_config(configuration: pg_sys::Oid) {
    config::drop_config(configuration)
}
#[pg_extern(sql = false)]
fn analysis_info(configuration: pg_sys::Oid) -> JsonB {
    JsonB(config::info(&config::Snapshot::load(configuration)))
}

#[pg_extern(sql = false)]
fn query_any(text: AnyElement, configuration: pg_sys::Oid) -> Internal {
    // SAFETY: Explicit SQL signature supplies a live text Datum. Preflight
    // precedes detoasting and PG analysis; the config snapshot is fully owned.
    let input = unsafe { pg_ffi::text_input(text) };
    pg_ffi::checked(analysis::check_input(input));
    drop(config::Snapshot::load(configuration));
    pg_ffi::plain_query_any(configuration, text.datum())
}

#[pg_extern(sql = false)]
#[allow(
    clippy::type_complexity,
    reason = "SQL columns remain explicit at the pgrx boundary"
)]
fn analyze(
    text: AnyElement,
    configuration: pg_sys::Oid,
) -> TableIterator<
    'static,
    (
        name!(raw, String),
        name!(prepared, Option<String>),
        name!(terms, Vec<String>),
        name!(kind, String),
        name!(script, String),
        name!(charabia_language, Option<String>),
        name!(language, Option<String>),
        name!(language_source, String),
        name!(byte_start, i64),
        name!(byte_end, i64),
        name!(char_start, i64),
        name!(char_end, i64),
        name!(route, String),
        name!(stemmer, Option<String>),
    ),
> {
    // SAFETY: Explicit SQL text input is owned for this complete callback.
    let input = unsafe { pg_ffi::text_input(text) };
    pg_ffi::checked(analysis::check_input(input));
    let snapshot = config::Snapshot::load(configuration);
    let spans = pg_ffi::checked(analysis::segment(input));
    let mut rows = Vec::with_capacity(spans.len());
    for span in spans {
        pgrx::check_for_interrupts!();
        let raw = &input[span.start..span.end];
        let (prepared, terms, kind) = if span.route.is_display() {
            (None, Vec::new(), "display_only".to_owned())
        } else {
            let observed = pg_ffi::cached_inspect(snapshot.dictionary(span.route), raw, span.route);
            let kind = match &observed.outcome {
                analysis::LexizeOutcome::Stop => "stop",
                analysis::LexizeOutcome::Terms { .. } => "word",
                analysis::LexizeOutcome::Ignore => match observed.kind {
                    charabia::TokenKind::Separator(charabia::SeparatorKind::Soft) => {
                        "separator_soft"
                    }
                    charabia::TokenKind::Separator(charabia::SeparatorKind::Hard) => {
                        "separator_hard"
                    }
                    _ => "ignore",
                },
            }
            .to_owned();
            (observed.prepared, observed.outcome.into_terms(), kind)
        };
        rows.push((
            raw.to_owned(),
            prepared,
            terms,
            kind,
            format!("{:?}", span.script),
            span.charabia_language.map(|l| l.code().to_owned()),
            if span.route.is_display() {
                None
            } else {
                Some(span.route.alias().to_owned())
            },
            span.language_source.as_str().to_owned(),
            span.start as i64,
            span.end as i64,
            span.char_start as i64,
            span.char_end as i64,
            span.route.alias().to_owned(),
            if snapshot.settings.stemming {
                span.route.algorithm().map(|a| format!("{a:?}"))
            } else {
                None
            },
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

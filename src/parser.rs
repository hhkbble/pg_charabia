use crate::{analysis, pg_ffi};
use pgrx::{Internal, PgSqlErrorCode, pg_extern};

pub struct ParserState {
    input: *const u8,
    spans: Vec<analysis::Span>,
    cursor: usize,
}

#[cfg(feature = "test-hooks")]
impl Drop for ParserState {
    fn drop(&mut self) {
        crate::test_hooks::parser_dropped();
    }
}

#[pg_extern(sql = false)]
fn linguistic_parser_start(input: Internal, length: i32) -> Internal {
    start(input, length, analysis::AnalysisMode::Linguistic)
}

#[pg_extern(sql = false)]
fn structured_parser_start(input: Internal, length: i32) -> Internal {
    start(input, length, analysis::AnalysisMode::Structured)
}

fn start(input: Internal, length: i32, mode: analysis::AnalysisMode) -> Internal {
    let pointer = pg_ffi::raw::<u8>(input).cast_const();
    // SAFETY: START's PG input remains alive through all GETTOKEN calls and END.
    let text = unsafe { pg_ffi::input(pointer, length) };
    pgrx::check_for_interrupts!();
    let spans = pg_ffi::checked(analysis::segment(text, mode));
    pgrx::check_for_interrupts!();
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::parser_created();
    // SAFETY: The state owns its Vec, its Drop does not call PG, and GETTOKEN /
    // END use exactly Option<ParserState> within the original input lifetime.
    let state = unsafe {
        pg_ffi::owned_state(Some(ParserState {
            input: pointer,
            spans,
            cursor: 0,
        }))
    };
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("start");
    state
}

#[pg_extern(sql = false)]
fn parser_gettoken(mut state: Internal, token: Internal, length: Internal) -> Internal {
    pgrx::check_for_interrupts!();
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("gettoken");
    let next = {
        // SAFETY: Paired START allocated precisely Option<ParserState>. Copy
        // the output descriptor before any subsequent PostgreSQL operation.
        let slot = unsafe { pg_ffi::state_mut::<Option<ParserState>>(&mut state) };
        slot.as_mut().map(|state| {
            state.spans.get(state.cursor).map(|span| {
                let next = (state.input, span.start, span.end, span.token_type);
                state.cursor += 1;
                next
            })
        })
    };
    let Some(next) = next else {
        pg_ffi::fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "parser already ended",
        );
    };
    let Some((input, start, end, route)) = next else {
        return pg_ffi::datum(0);
    };
    // SAFETY: Validated spans reference the original still-live PG input.
    unsafe { pg_ffi::token_output(token, length, input, start, end) };
    pg_ffi::datum(route.id() as usize)
}

#[pg_extern(sql = false)]
fn parser_end(mut state: Internal) {
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("end");
    // SAFETY: Paired START allocated this type. Taking it frees the large state;
    // the context callback later drops the empty Option container exactly once.
    unsafe { pg_ffi::state_mut::<Option<ParserState>>(&mut state) }.take();
}

#[pg_extern(sql = false)]
fn linguistic_parser_lextypes(_unused: Internal) -> Internal {
    pg_ffi::lextypes(analysis::AnalysisMode::Linguistic)
}

#[pg_extern(sql = false)]
fn structured_parser_lextypes(_unused: Internal) -> Internal {
    pg_ffi::lextypes(analysis::AnalysisMode::Structured)
}

#[pg_extern(sql = false)]
fn parser_headline(prs: Internal, options: Internal, query: Internal) -> Internal {
    // SAFETY: PG's HEADLINE contract supplies live request-owned structures;
    // SQL tsquery is passed by reference and no pointer escapes the callback.
    unsafe {
        pg_ffi::pointer(pg_ffi::headline(
            pg_ffi::raw(prs),
            pg_ffi::raw(options),
            pg_ffi::raw(query),
        ))
    }
}

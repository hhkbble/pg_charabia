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
fn parser_start_v1(input: Internal, length: i32) -> Internal {
    let pointer = pg_ffi::raw::<u8>(input).cast_const();
    // SAFETY: START's PG input remains alive through all GETTOKEN calls and END.
    let text = unsafe { pg_ffi::input(pointer, length) };
    pgrx::check_for_interrupts!();
    let spans = pg_ffi::checked(analysis::segment(text));
    pgrx::check_for_interrupts!();
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::parser_created();
    let state = Internal::new(Some(ParserState {
        input: pointer,
        spans,
        cursor: 0,
    }));
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("start");
    state
}

#[pg_extern(sql = false)]
fn parser_gettoken_v1(mut state: Internal, token: Internal, length: Internal) -> Internal {
    pgrx::check_for_interrupts!();
    // SAFETY: Paired START allocated precisely Option<ParserState>.
    let slot = unsafe { pg_ffi::state_mut::<Option<ParserState>>(&mut state) };
    let state = slot.as_mut().unwrap_or_else(|| {
        pg_ffi::fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "parser already ended",
        )
    });
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("gettoken");
    let Some(span) = state.spans.get(state.cursor) else {
        return pg_ffi::datum(0);
    };
    // SAFETY: Validated spans reference the original still-live PG input.
    unsafe { pg_ffi::token_output(token, length, state.input, span.start, span.end) };
    state.cursor += 1;
    pg_ffi::datum(span.route.id() as usize)
}

#[pg_extern(sql = false)]
fn parser_end_v1(mut state: Internal) {
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("end");
    // SAFETY: Paired START allocated this type. Taking it frees the large state;
    // the context callback later drops the empty Option container exactly once.
    unsafe { pg_ffi::state_mut::<Option<ParserState>>(&mut state) }.take();
}

#[pg_extern(sql = false)]
fn parser_lextypes_v1(_unused: Internal) -> Internal {
    pg_ffi::lextypes()
}

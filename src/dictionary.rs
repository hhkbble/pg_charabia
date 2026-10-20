use crate::{analysis, pg_ffi, profile::NormalizationRoute};
use pgrx::{Internal, pg_extern};

struct DictionaryState {
    route: NormalizationRoute,
    options: analysis::Options,
}

#[cfg(feature = "test-hooks")]
impl Drop for DictionaryState {
    fn drop(&mut self) {
        crate::test_hooks::dictionary_dropped();
    }
}

#[pg_extern(sql = false)]
fn dictionary_init_v1(options: Internal) -> Internal {
    // SAFETY: Template INIT receives a live PG List<DefElem> in dictCtx or the
    // DDL validation context; Internal attaches Rust cleanup to that context.
    let (route, options) = unsafe { pg_ffi::dictionary_options(options) };
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::dictionary_created();
    let state = Internal::new(DictionaryState { route, options });
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("init");
    state
}

#[pg_extern(sql = false)]
fn dictionary_lexize_v1(
    state: Internal,
    token: Internal,
    length: Internal,
    _substate: Internal,
) -> Internal {
    // SAFETY: PG owns token bytes for this callback. Length is a by-value i32
    // Datum despite the SQL internal type; this also validates direct ts_lexize.
    let text = unsafe { pg_ffi::input(pg_ffi::raw::<u8>(token), pg_ffi::by_value_length(length)) };
    // SAFETY: The paired INIT created exactly DictionaryState in a live context.
    let state = unsafe { pg_ffi::state::<DictionaryState>(&state) };
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("lexize");
    pgrx::check_for_interrupts!();
    pg_ffi::lexemes(pg_ffi::checked(analysis::normalize(
        text,
        state.route,
        &state.options,
    )))
}

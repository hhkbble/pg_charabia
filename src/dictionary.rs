use crate::{analysis, pg_ffi};
use pgrx::{Internal, pg_extern};

pub(crate) struct DictionaryState {
    pub(crate) options: analysis::Options,
    #[cfg(feature = "test-hooks")]
    generation: u64,
}

#[cfg(feature = "test-hooks")]
impl Drop for DictionaryState {
    fn drop(&mut self) {
        crate::test_hooks::dictionary_dropped(
            fst_bytes(&self.options),
            self.options.referenced_fst_bytes(),
        );
    }
}

#[cfg(feature = "test-hooks")]
fn fst_bytes(options: &analysis::Options) -> usize {
    options.fst_bytes()
}

#[pg_extern(sql = false)]
fn dictionary_init(options: Internal) -> Internal {
    // SAFETY: Template INIT receives a live PG List<DefElem> in dictCtx or the
    // DDL validation context; owned_state attaches cleanup to that context.
    pgrx::check_for_interrupts!();
    let options = unsafe { pg_ffi::dictionary_options(options) };
    pgrx::check_for_interrupts!();
    #[cfg(feature = "test-hooks")]
    let generation =
        crate::test_hooks::dictionary_created(fst_bytes(&options), options.referenced_fst_bytes());
    // SAFETY: The state borrows static FST bytes, Drop is pure Rust, and LEXIZE borrows the
    // exact type only while PG keeps the owning dictCtx alive.
    let state = unsafe {
        pg_ffi::owned_state(DictionaryState {
            options,
            #[cfg(feature = "test-hooks")]
            generation,
        })
    };
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("init");
    state
}

#[pg_extern(sql = false)]
fn dictionary_lexize(
    state: Internal,
    token: Internal,
    length: Internal,
    _substate: Internal,
) -> Internal {
    // SAFETY: PG owns token bytes for this callback. Length is a by-value i32
    // Datum despite the SQL internal type; this also validates direct ts_lexize.
    let text = unsafe { pg_ffi::input(pg_ffi::raw::<u8>(token), pg_ffi::by_value_length(length)) };
    pgrx::check_for_interrupts!();
    #[cfg(feature = "test-hooks")]
    {
        // End the cache-state borrow before a hook can invoke PostgreSQL.
        let generation = unsafe { pg_ffi::state::<DictionaryState>(&state) }.generation;
        crate::test_hooks::dictionary_lexized(generation);
        crate::test_hooks::hit("lexize");
    }
    let outcome = {
        // SAFETY: The paired INIT created exactly DictionaryState in a live
        // context. Only pure Rust analysis runs while this borrow is live.
        let state = unsafe { pg_ffi::state::<DictionaryState>(&state) };
        analysis::normalize(text, state.options.language(), &state.options)
    };
    pgrx::check_for_interrupts!();
    pg_ffi::lexemes(pg_ffi::checked(outcome))
}

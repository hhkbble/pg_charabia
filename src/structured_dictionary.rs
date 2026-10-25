//! Standard terminal Dictionary callbacks; state never borrows Parser metadata.
use crate::{pg_ffi, structured_tokens};
use pgrx::{Internal, pg_extern};

struct StructuredDictionaryState {
    options: structured_tokens::Options,
    #[cfg(feature = "test-hooks")]
    generation: u64,
}

#[cfg(feature = "test-hooks")]
impl Drop for StructuredDictionaryState {
    fn drop(&mut self) {
        crate::test_hooks::dictionary_dropped(0, self.options.referenced_fst_bytes());
    }
}

#[pg_extern(sql = false)]
fn structured_dictionary_init(options: Internal) -> Internal {
    pgrx::check_for_interrupts!();
    // SAFETY: The template supplies a live PG List<DefElem> in the owning context.
    let options = unsafe { pg_ffi::structured_options(options) };
    pgrx::check_for_interrupts!();
    #[cfg(feature = "test-hooks")]
    let generation = crate::test_hooks::dictionary_created(0, options.referenced_fst_bytes());
    // SAFETY: Paired LEXIZE uses this exact type. Drop only uses the checked
    // allocator's PG deallocation path. Static FST bytes outlive every context.
    // Registration retains RAII on failure.
    let state = unsafe {
        pg_ffi::owned_state(StructuredDictionaryState {
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
fn structured_dictionary_lexize(
    state: Internal,
    token: Internal,
    length: Internal,
    _substate: Internal,
) -> Internal {
    // SAFETY: PG owns the current input. Filters and direct calls use the same
    // independent validation, including the by-value length Datum.
    let text = unsafe { pg_ffi::input(pg_ffi::raw::<u8>(token), pg_ffi::by_value_length(length)) };
    pgrx::check_for_interrupts!();
    #[cfg(feature = "test-hooks")]
    {
        // SAFETY: Paired INIT created exactly this state. End its borrow before a hook can call PG.
        let generation = unsafe { pg_ffi::state::<StructuredDictionaryState>(&state) }.generation;
        crate::test_hooks::dictionary_lexized(generation);
        crate::test_hooks::hit("lexize");
    }
    let outcome = {
        // SAFETY: The paired INIT allocated this state. Analysis only calls PG
        // through the checked allocator while borrowing its options. Those
        // calls cannot invalidate dictionary caches or subsidiary objects.
        let options = &unsafe { pg_ffi::state::<StructuredDictionaryState>(&state) }.options;
        structured_tokens::analyze(text, options)
    };
    pgrx::check_for_interrupts!();
    pg_ffi::structured_lexemes(pg_ffi::checked(outcome))
}

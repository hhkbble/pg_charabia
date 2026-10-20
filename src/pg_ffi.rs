use std::{
    ffi::{CStr, c_char, c_void},
    ptr, slice, str,
};

use pgrx::{
    AnyElement, FromDatum, Internal, JsonB, PgLogLevel, PgSqlErrorCode, Spi, ereport, pg_sys,
};

use crate::analysis::{AnalysisError, ErrorKind, LexizeOutcome, MAX_INPUT_BYTES};
use crate::routes::{Route, TYPE_COUNT};
use crate::settings::DictionaryParameters;

pub fn fail(code: PgSqlErrorCode, message: impl AsRef<str>) -> ! {
    ereport!(PgLogLevel::ERROR, code, message.as_ref().to_owned());
    unreachable!()
}

pub fn checked<T>(result: Result<T, AnalysisError>) -> T {
    result.unwrap_or_else(|error| {
        let code = match error.kind {
            ErrorKind::InvalidInput => PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
            ErrorKind::Limit => PgSqlErrorCode::ERRCODE_PROGRAM_LIMIT_EXCEEDED,
            ErrorKind::Internal => PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
        };
        fail(code, error.message)
    })
}

pub fn require_utf8() {
    // SAFETY: Executed only in a PostgreSQL backend through guarded callbacks.
    if unsafe { pg_sys::GetDatabaseEncoding() } != pg_sys::pg_enc::PG_UTF8 as i32 {
        fail(
            PgSqlErrorCode::ERRCODE_FEATURE_NOT_SUPPORTED,
            "pg_charabia requires UTF8 server encoding",
        );
    }
}

pub fn datum(value: usize) -> Internal {
    Internal::from(Some(pg_sys::Datum::from(value)))
}

pub fn pointer<T>(value: *mut T) -> Internal {
    Internal::from(Some(pg_sys::Datum::from(value)))
}

/// # Safety
/// Call only in a live backend context. `T` must own its Rust resources and its
/// Drop implementation must neither panic nor invoke PostgreSQL. The paired
/// callbacks must use this exact type and respect the context's lifetime.
pub unsafe fn owned_state<T: 'static>(value: T) -> Internal {
    #[pgrx::pg_guard]
    unsafe extern "C-unwind" fn drop_state<T>(value: *mut c_void) {
        // SAFETY: Registration transfers exactly one Box<T> to this callback.
        unsafe { drop(Box::from_raw(value.cast::<T>())) };
        #[cfg(feature = "test-hooks")]
        crate::test_hooks::state_released();
    }

    let owner = Box::new(value);
    // SAFETY: The caller runs in the original live PG callback context.
    let context = unsafe { pg_sys::CurrentMemoryContext };
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("register_alloc");
    // SAFETY: PG owns callback storage. Retaining the Box until allocation and
    // registration succeed also frees it when a guarded PG allocation errors.
    let callback = unsafe {
        pg_sys::MemoryContextAllocZero(
            context,
            std::mem::size_of::<pg_sys::MemoryContextCallback>(),
        )
        .cast::<pg_sys::MemoryContextCallback>()
    };
    // SAFETY: The allocation is aligned and large enough for this callback.
    unsafe {
        (*callback).func = Some(drop_state::<T>);
        (*callback).arg = ptr::null_mut();
    }
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("register_link");
    // SAFETY: PG18 registration only links the initialized callback; it cannot
    // ERROR. No fallible operation is allowed before the immediate handoff.
    unsafe { pg_sys::MemoryContextRegisterResetCallback(context, callback) };
    let state = Box::into_raw(owner);
    // SAFETY: Use the pointer returned by into_raw, not a borrow-derived pointer
    // from before the Box moved. No PG call or fallible operation intervenes,
    // so reset cannot observe the callback's temporary null argument.
    unsafe { (*callback).arg = state.cast() };
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::state_registered();
    pointer(state)
}

pub fn raw<T>(value: Internal) -> *mut T {
    value
        .unwrap()
        .unwrap_or_else(|| {
            fail(
                PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                "unexpected SQL NULL internal argument",
            )
        })
        .cast_mut_ptr()
}

pub fn by_value_length(value: Internal) -> i32 {
    value
        .unwrap()
        .unwrap_or_else(|| {
            fail(
                PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                "missing internal length",
            )
        })
        .value() as i32
}

/// # Safety
/// `pointer` must identify PG-owned bytes valid for the returned borrow. The
/// caller must not keep the borrow beyond the current callback/input lifetime.
pub unsafe fn input<'a>(pointer: *const u8, length: i32) -> &'a str {
    require_utf8();
    let length = usize::try_from(length).unwrap_or_else(|_| {
        fail(
            PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
            "negative token length",
        )
    });
    if length > MAX_INPUT_BYTES {
        fail(
            PgSqlErrorCode::ERRCODE_PROGRAM_LIMIT_EXCEEDED,
            "pg_charabia input exceeds 262144 bytes",
        );
    }
    if length == 0 {
        return "";
    }
    if pointer.is_null() {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "NULL pointer for nonempty input",
        );
    }
    // SAFETY: The FTS caller owns `length` bytes for this callback; bounds above
    // reject negative, excessive and nonempty-null ranges before constructing it.
    let bytes = unsafe { slice::from_raw_parts(pointer, length) };
    str::from_utf8(bytes).unwrap_or_else(|_| {
        fail(
            PgSqlErrorCode::ERRCODE_CHARACTER_NOT_IN_REPERTOIRE,
            "invalid UTF8 input",
        )
    })
}

/// # Safety
/// `value` is a live text Datum supplied by the diagnostic SQL callback. The
/// returned borrow must not outlive this callback's current memory context.
pub unsafe fn text_input<'a>(value: AnyElement) -> &'a str {
    require_utf8();
    if value.oid() != pg_sys::TEXTOID {
        fail(
            PgSqlErrorCode::ERRCODE_DATATYPE_MISMATCH,
            "diagnostic callback requires a text Datum",
        );
    }
    // SAFETY: The checked text OID identifies a varlena Datum. This size helper
    // reads TOAST metadata without first allocating the full detoasted payload.
    let size = unsafe { pg_sys::toast_raw_datum_size(value.datum()) };
    if size > MAX_INPUT_BYTES + pg_sys::VARHDRSZ {
        fail(
            PgSqlErrorCode::ERRCODE_PROGRAM_LIMIT_EXCEEDED,
            "pg_charabia input exceeds 262144 bytes",
        );
    }
    // SAFETY: PG owns the original or detoasted allocation in the current
    // context; packed varlena helpers support both short and ordinary headers.
    let text = unsafe { pg_sys::pg_detoast_datum_packed(value.datum().cast_mut_ptr()) };
    let length = unsafe { pgrx::varlena::varsize_any_exhdr(text) };
    let data = unsafe { pgrx::varlena::vardata_any(text) };
    let length = i32::try_from(length).unwrap_or_else(|_| {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "text Datum length overflow",
        )
    });
    // SAFETY: The detoasted PG allocation contains this many live bytes.
    unsafe { input(data.cast(), length) }
}

/// # Safety
/// PG calls the registered method with this exact state type, and no other
/// Rust borrow of this state may be live during this callback.
pub unsafe fn state_mut<T>(value: &mut Internal) -> &mut T {
    // SAFETY: State is created with owned_state(T) by the paired method.
    unsafe { value.get_mut::<T>() }.unwrap_or_else(|| {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "missing callback state",
        )
    })
}

/// # Safety
/// PG calls this method with a state created by the paired INIT. Its context
/// remains alive and no mutable borrow is active during this call.
pub unsafe fn state<T>(value: &Internal) -> &T {
    // SAFETY: Caller guarantees the allocation's type and context lifetime.
    unsafe { value.get::<T>() }.unwrap_or_else(|| {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "missing dictionary state",
        )
    })
}

fn pg_string(value: &str) -> *mut c_char {
    if value.as_bytes().contains(&0) {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "unexpected NUL in PG string output",
        );
    }
    // SAFETY: palloc owns the result in CurrentMemoryContext; both the bytes and
    // the final NUL fit the allocated range. The caller transfers ownership to PG.
    unsafe {
        let result = pg_sys::palloc(value.len() + 1).cast::<u8>();
        ptr::copy_nonoverlapping(value.as_ptr(), result, value.len());
        *result.add(value.len()) = 0;
        result.cast()
    }
}

pub fn lexemes(outcome: LexizeOutcome) -> Internal {
    let (first, second) = match outcome {
        LexizeOutcome::Ignore => return datum(0),
        LexizeOutcome::Stop => (None, None),
        LexizeOutcome::Terms { first, second } => (Some(first), second),
    };
    // SAFETY: PG owns the zero-terminated array and each separately allocated
    // string. Distinct nvariant values express alternatives at the same position.
    unsafe {
        let result =
            pg_sys::palloc0(std::mem::size_of::<pg_sys::TSLexeme>() * 3).cast::<pg_sys::TSLexeme>();
        if let Some(term) = first {
            (*result).lexeme = pg_string(&term);
        }
        if let Some(term) = second {
            (*result.add(1)).lexeme = pg_string(&term);
            (*result.add(1)).nvariant = 1;
        }
        pointer(result)
    }
}

pub fn lextypes() -> Internal {
    // SAFETY: Each descriptor/string and the zero terminator are PG-owned.
    unsafe {
        let result = pg_sys::palloc0(std::mem::size_of::<pg_sys::LexDescr>() * (TYPE_COUNT + 1))
            .cast::<pg_sys::LexDescr>();
        for (index, route) in Route::ALL.into_iter().enumerate() {
            let entry = result.add(index);
            (*entry).lexid = route.id();
            (*entry).alias = pg_string(route.alias());
            (*entry).descr = pg_string(if route.is_display() {
                "Unindexed original text fragment"
            } else {
                route.alias()
            });
        }
        pointer(result)
    }
}

/// # Safety
/// Both out-pointers and the original input are owned by the FTS caller. The
/// pointer outputs must remain valid until the paired END invocation.
pub unsafe fn token_output(
    token: Internal,
    length: Internal,
    input: *const u8,
    start: usize,
    end: usize,
) {
    let out_token = raw::<*mut c_char>(token);
    let out_length = raw::<i32>(length);
    if out_token.is_null() || out_length.is_null() {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "NULL token output pointer",
        );
    }
    let size = i32::try_from(end - start).unwrap_or_else(|_| {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "token length overflow",
        )
    });
    // SAFETY: segment() validated UTF8 bounds; the input outlives parser state.
    unsafe {
        *out_token = input.add(start).cast_mut().cast();
        *out_length = size;
    }
}

pub fn invalid(message: impl AsRef<str>) -> ! {
    fail(PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE, message)
}

/// # Safety
/// `options` is the PG-owned List<DefElem> from INIT or deserialize_deflist.
pub unsafe fn option_strings(options: *mut pg_sys::List) -> Vec<(String, String)> {
    pgrx::memcx::current_context(|cx| {
        // SAFETY: Caller supplies the documented live List<DefElem> or NIL.
        let list = unsafe { pgrx::list::List::<*mut c_void>::downcast_ptr_in_memcx(options, cx) }
            .unwrap_or_else(|| invalid("invalid dictionary option list"));
        let mut result = Vec::new();
        for pointer in list.iter() {
            let def = (*pointer).cast::<pg_sys::DefElem>();
            // SAFETY: PG owns all option nodes/strings; defGetString is guarded.
            let name = unsafe { CStr::from_ptr((*def).defname) }
                .to_str()
                .unwrap_or_else(|_| invalid("invalid option name UTF8"))
                .to_owned();
            let value = unsafe { CStr::from_ptr(pg_sys::defGetString(def)) }
                .to_str()
                .unwrap_or_else(|_| invalid("invalid option value UTF8"))
                .to_owned();
            result.push((name, value));
        }
        result
    })
}

/// # Safety
/// `options` is the INIT callback's live List<DefElem>.
pub unsafe fn dictionary_options(options: Internal) -> DictionaryParameters {
    require_utf8();
    // SAFETY: The paired SQL template supplies the option list.
    crate::config::parameters_from_strings(unsafe { option_strings(raw(options)) })
}

pub fn options_from_catalog(options: &str) -> DictionaryParameters {
    require_utf8();
    // SAFETY: PG text/list are request-owned; deserialize_deflist is a guarded
    // binding. Rust outputs own their values before the context can change.
    unsafe {
        let text = pg_sys::cstring_to_text(pg_string(options));
        let list = pg_sys::deserialize_deflist(pg_sys::Datum::from(text));
        crate::config::parameters_from_strings(option_strings(list))
    }
}

pub fn require_owner(class: pg_sys::Oid, id: pg_sys::Oid) {
    // SAFETY: IDs come from a verified catalog snapshot; this is a backend call.
    if !unsafe { pg_sys::object_ownercheck(class, id, pg_sys::GetUserId()) } {
        fail(
            PgSqlErrorCode::ERRCODE_INSUFFICIENT_PRIVILEGE,
            "must own the configuration and its dictionaries",
        );
    }
}

/// Read one owned JSONB value from current catalogs after a management lock.
/// Ordinary SPI under REPEATABLE READ can retain a snapshot from before a
/// concurrent COPY committed. Only management validation uses this fresh view.
pub fn latest_catalog_json(sql: &str) -> Option<serde_json::Value> {
    Spi::connect(|_| {
        // SAFETY: The private caller provides a single catalog SELECT returning
        // JSONB. SPI owns plan, snapshot stack and tuples until this connection
        // finishes; JsonB conversion creates an independent Rust value.
        unsafe {
            let plan = pg_sys::SPI_prepare(pg_string(sql), 0, ptr::null_mut());
            if plan.is_null() {
                invalid("could not prepare configuration catalog query");
            }
            // Explicit read-only snapshots do not advance the command ID. This
            // also makes the last DDL in this command visible to the reload.
            pg_sys::CommandCounterIncrement();
            let snapshot = pg_sys::GetLatestSnapshot();
            let status = pg_sys::SPI_execute_snapshot(
                plan,
                ptr::null_mut(),
                ptr::null(),
                snapshot,
                ptr::null_mut(),
                true,
                false,
                1,
            );
            if status != pg_sys::SPI_OK_SELECT as i32 {
                invalid("configuration catalog query was not a SELECT");
            }
            let table = pg_sys::SPI_tuptable;
            if table.is_null() || (*table).numvals == 0 {
                return None;
            }
            let mut is_null = false;
            let datum = pg_sys::SPI_getbinval(*(*table).vals, (*table).tupdesc, 1, &mut is_null);
            JsonB::from_datum(datum, is_null).map(|json| json.0)
        }
    })
}

pub fn lock_object(class: pg_sys::Oid, id: pg_sys::Oid) {
    // SAFETY: Called inside a management statement. PG's resource owner holds
    // the object lock through transaction end, including ERROR/savepoint paths.
    unsafe { pg_sys::LockDatabaseObject(class, id, 0, pg_sys::AccessExclusiveLock as _) };
}

pub fn cached_inspect(id: pg_sys::Oid, text: &str, route: Route) -> crate::analysis::TokenAnalysis {
    // SAFETY: Lookup returns a valid initialized dictionary entry. The Rust
    // state borrow is confined to pure analysis; no PG call can reset dictCtx.
    unsafe {
        let entry = pg_sys::lookup_ts_dictionary_cache(id);
        if entry.is_null() || !(*entry).isvalid || (*entry).dictData.is_null() {
            invalid("invalid dictionary cache entry");
        }
        let outcome = {
            let state = &*(*entry)
                .dictData
                .cast::<crate::dictionary::DictionaryState>();
            if state.route != route {
                Err(AnalysisError::new(
                    ErrorKind::Internal,
                    "cached dictionary route mismatch",
                ))
            } else {
                crate::analysis::inspect(text, route, &state.options)
            }
        };
        pgrx::check_for_interrupts!();
        checked(outcome)
    }
}

pub fn plain_query_any(config: pg_sys::Oid, text: pg_sys::Datum) -> Internal {
    // SAFETY: plainto_tsquery_byid always returns a fresh request-owned query.
    // Only that object is mutated; callers cannot provide a shared TSQuery.
    unsafe {
        let result = pgrx::direct_function_call_as_datum(
            pg_sys::plainto_tsquery_byid,
            &[Some(pg_sys::Datum::from(config)), Some(text)],
        )
        .unwrap_or_else(|| invalid("plain query returned NULL"));
        let query = result.cast_mut_ptr::<pg_sys::TSQueryData>();
        let size = (*query).size;
        if size < 0 {
            invalid("invalid plain query size");
        }
        let items = (*query).data.as_mut_ptr().cast::<pg_sys::QueryItem>();
        for i in 0..size as usize {
            let item = *items.add(i);
            if (item.type_ as u32) == pg_sys::QI_OPR
                && ![pg_sys::OP_AND, pg_sys::OP_OR].contains(&(item.qoperator.oper as u32))
            {
                invalid("non-plain operator returned by analyzer");
            }
            if ![pg_sys::QI_VAL, pg_sys::QI_OPR].contains(&(item.type_ as u32)) {
                invalid("unexpected plain query item");
            }
        }
        for i in 0..size as usize {
            let item = &mut *items.add(i);
            if (item.type_ as u32) == pg_sys::QI_OPR
                && (item.qoperator.oper as u32) == pg_sys::OP_AND
            {
                item.qoperator.oper = pg_sys::OP_OR as i8;
            }
        }
        Internal::from(Some(result))
    }
}

fn headline_checked<T>(result: Result<T, crate::headline::Error>) -> T {
    result.unwrap_or_else(|error| {
        let code = match error.kind {
            crate::headline::ErrorKind::InvalidInput => {
                PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE
            }
            crate::headline::ErrorKind::Limit => PgSqlErrorCode::ERRCODE_PROGRAM_LIMIT_EXCEEDED,
            crate::headline::ErrorKind::Unsupported => {
                PgSqlErrorCode::ERRCODE_FEATURE_NOT_SUPPORTED
            }
        };
        fail(code, error.message)
    })
}

/// # Safety
/// All pointers are supplied by the live PG18 HEADLINE call. The request owns
/// their storage; no reference or pointer escapes this synchronous callback.
pub unsafe fn headline(
    prs: *mut pg_sys::HeadlineParsedText,
    options: *mut pg_sys::List,
    query: pg_sys::TSQuery,
) -> *mut pg_sys::HeadlineParsedText {
    require_utf8();
    if prs.is_null() || query.is_null() {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "NULL headline state/query",
        );
    }
    // SAFETY: The core supplied live PG structures. Empty arrays may be NULL.
    let count = unsafe { (*prs).curwords };
    let capacity = unsafe { (*prs).lenwords };
    if count < 0 || capacity < count || (count > 0 && unsafe { (*prs).words.is_null() }) {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "invalid headline entry array",
        );
    }
    // SAFETY: A detoasted TSQuery has an aligned QueryItem array immediately
    // after its fixed-size header. Core already validated the query object.
    let query_size = unsafe { (*query).size };
    if query_size < 0 {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "invalid headline query size",
        );
    }
    let items = unsafe { (*query).data.as_ptr().cast::<pg_sys::QueryItem>() };
    for index in 0..query_size as usize {
        // SAFETY: The discriminant selects the active union field.
        let item = unsafe { *items.add(index) };
        match unsafe { item.type_ } as u32 {
            pg_sys::QI_VAL => {
                if unsafe { item.qoperand.weight } != 0 {
                    fail(
                        PgSqlErrorCode::ERRCODE_FEATURE_NOT_SUPPORTED,
                        "headline does not support weighted query operands",
                    );
                }
            }
            pg_sys::QI_OPR => {}
            _ => fail(
                PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                "invalid headline query item",
            ),
        }
    }
    let policy = pgrx::memcx::current_context(|cx| {
        // SAFETY: deserialize_deflist supplies a valid List<DefElem> or NIL.
        let list = unsafe { pgrx::list::List::<*mut c_void>::downcast_ptr_in_memcx(options, cx) }
            .unwrap_or_else(|| {
                fail(
                    PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                    "invalid headline option list",
                )
            });
        let values = list.iter().map(|pointer| {
            let def = (*pointer).cast::<pg_sys::DefElem>();
            if def.is_null() {
                fail(
                    PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                    "NULL headline option",
                );
            }
            // SAFETY: PG owns NUL-terminated option strings; defGetString uses
            // pgrx's guarded PG binding, so ERROR unwinds Rust owners safely.
            let name = unsafe { CStr::from_ptr((*def).defname) }
                .to_str()
                .unwrap_or_else(|_| {
                    fail(
                        PgSqlErrorCode::ERRCODE_CHARACTER_NOT_IN_REPERTOIRE,
                        "invalid UTF8 headline option name",
                    )
                })
                .to_owned();
            let value = unsafe { CStr::from_ptr(pg_sys::defGetString(def)) }
                .to_str()
                .unwrap_or_else(|_| {
                    fail(
                        PgSqlErrorCode::ERRCODE_CHARACTER_NOT_IN_REPERTOIRE,
                        "invalid UTF8 headline option value",
                    )
                })
                .to_owned();
            (name, value)
        });
        headline_checked(crate::headline::Options::parse(values))
    });
    let mut selector_options = ptr::null_mut();
    for (name, value) in policy.selector_options() {
        // SAFETY: All option nodes/strings are PG allocations in the request;
        // Escape is intentionally omitted and ShortWord is always exactly 0.
        unsafe {
            let node = pg_sys::makeString(pg_string(&value));
            let def = pg_sys::makeDefElem(pg_string(name), node.cast(), -1);
            selector_options = pg_sys::lappend(selector_options, def.cast());
        }
    }
    let size = (count as usize)
        .checked_mul(std::mem::size_of::<pg_sys::HeadlineWordEntry>())
        .unwrap_or_else(|| {
            fail(
                PgSqlErrorCode::ERRCODE_PROGRAM_LIMIT_EXCEEDED,
                "headline scratch allocation overflow",
            )
        });
    // SAFETY: PG allocator returns correctly aligned storage. A zero-sized
    // allocation remains valid; no zero-length NULL slice is constructed.
    let scratch_words = unsafe { pg_sys::palloc(size.max(1)).cast::<pg_sys::HeadlineWordEntry>() };
    if count > 0 {
        // SAFETY: Both arrays hold count entries; this is a shallow copy.
        unsafe { ptr::copy_nonoverlapping((*prs).words, scratch_words, count as usize) };
    }
    for index in 0..count as usize {
        pgrx::check_for_interrupts!();
        // SAFETY: Each entry and its len bytes belong to the core request.
        let entry = unsafe { *scratch_words.add(index) };
        let route = crate::routes::Route::from_id(entry.type_() as i32).unwrap_or_else(|| {
            fail(
                PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                "invalid headline token type",
            )
        });
        let raw = unsafe { input(entry.word.cast(), entry.len() as i32) };
        let word = if entry.repeated() != 0 || route.is_display() {
            false
        } else {
            let prepared = checked(crate::analysis::prepare(raw, route));
            matches!(prepared.kind, charabia::TokenKind::Word) && !prepared.key.is_empty()
        };
        // SAFETY: Mutate only scratch type/flags. The original pos, item,
        // repeated, raw bytes and language route remain owned by PG core.
        unsafe {
            let target = &mut *scratch_words.add(index);
            target.set_type(if word { 1 } else { 12 });
            target.set_selected(0);
            target.set_in(0);
            target.set_replace(0);
            target.set_skip(0);
        }
    }
    // SAFETY: Scratch has its own PG allocation; borrowed words/items are never
    // freed here. PG18 selector writes flags/markers without reallocating words.
    let scratch = unsafe {
        pg_sys::palloc(std::mem::size_of::<pg_sys::HeadlineParsedText>())
            .cast::<pg_sys::HeadlineParsedText>()
    };
    unsafe {
        ptr::write(scratch, *prs);
        (*scratch).words = scratch_words;
        (*scratch).lenwords = count;
        (*scratch).startsel = ptr::null_mut();
        (*scratch).stopsel = ptr::null_mut();
        (*scratch).fragdelim = ptr::null_mut();
        (*scratch).startsellen = 0;
        (*scratch).stopsellen = 0;
        (*scratch).fragdelimlen = 0;
    }
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("headline_selector");
    // SAFETY: pgrx calls the guarded PG binding using a private fcinfo; an ERROR
    // returns through its FFI barrier rather than jumping over Rust Drop.
    let selected = unsafe {
        pgrx::direct_function_call_as_datum(
            pg_sys::prsd_headline,
            &[
                Some(pg_sys::Datum::from(scratch)),
                Some(pg_sys::Datum::from(selector_options)),
                Some(pg_sys::Datum::from(query)),
            ],
        )
    }
    .unwrap_or_else(|| {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "headline selector returned SQL NULL",
        )
    });
    if selected.cast_mut_ptr::<pg_sys::HeadlineParsedText>() != scratch
        || unsafe { (*scratch).words != scratch_words || (*scratch).curwords != count }
    {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "headline selector replaced scratch entries",
        );
    }
    let output = (0..count as usize).map(|index| {
        pgrx::check_for_interrupts!();
        // SAFETY: The selector retained scratch_words and count above.
        let original = unsafe { *(*prs).words.add(index) };
        let selected = unsafe { *scratch_words.add(index) };
        if selected.skip() != 0 || selected.replace() != 0 {
            fail(
                PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                "headline projection unexpectedly skipped/replaced source text",
            );
        }
        let raw = unsafe { input(original.word.cast(), original.len() as i32) };
        crate::headline::OutputEntry {
            bytes: if original.repeated() == 0 {
                headline_checked(crate::headline::rendered_len(raw, policy.escape))
            } else {
                0
            },
            in_fragment: selected.in_() != 0,
            repeated: original.repeated() != 0,
            selected: selected.selected() != 0,
            replace: selected.replace() != 0,
            skip: selected.skip() != 0,
        }
    });
    headline_checked(crate::headline::output_size(output, &policy));
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("headline_escape");
    for index in 0..count as usize {
        pgrx::check_for_interrupts!();
        // SAFETY: Copy only selector flags into originals; no PG array borrow
        // remains live across the guarded palloc performed by pg_string.
        let original = unsafe { *(*prs).words.add(index) };
        let selected = unsafe { *scratch_words.add(index) };
        let escaped = if original.repeated() == 0 {
            let raw = unsafe { input(original.word.cast(), original.len() as i32) };
            let rendered = headline_checked(crate::headline::escape_html(raw, policy.escape));
            if matches!(rendered, std::borrow::Cow::Owned(_)) {
                Some((pg_string(&rendered), rendered.len()))
            } else {
                None
            }
        } else {
            None
        };
        // SAFETY: The old shared word buffers remain owned by request context.
        unsafe {
            let target = &mut *(*prs).words.add(index);
            target.set_selected(selected.selected());
            target.set_in(selected.in_());
            target.set_replace(selected.replace());
            target.set_skip(selected.skip());
            if let Some((word, length)) = escaped {
                target.word = word;
                target.set_len(length as u32);
            }
        }
    }
    // SAFETY: PG owns all marker allocations. Transfer them to core's state,
    // free only scratch structs, then let exactly one generateHeadline run.
    unsafe {
        (*prs).startsel = (*scratch).startsel;
        (*prs).stopsel = (*scratch).stopsel;
        (*prs).fragdelim = (*scratch).fragdelim;
        (*prs).startsellen = (*scratch).startsellen;
        (*prs).stopsellen = (*scratch).stopsellen;
        (*prs).fragdelimlen = (*scratch).fragdelimlen;
        pg_sys::pfree(scratch_words.cast());
        pg_sys::pfree(scratch.cast());
    }
    prs
}

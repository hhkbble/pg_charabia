use std::{
    ffi::{CStr, c_char, c_void},
    ptr, slice, str,
};

use pgrx::{AnyElement, Internal, PgLogLevel, PgSqlErrorCode, ereport, pg_sys};

use crate::analysis::{AnalysisError, ErrorKind, LexizeOutcome, MAX_INPUT_BYTES};
use crate::routes::{LanguageId, TYPE_COUNT, TokenType};

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
    let count = match &outcome {
        LexizeOutcome::Ignore => return datum(0),
        LexizeOutcome::Stop => 0,
        LexizeOutcome::Filter(_) => 1,
        LexizeOutcome::Terms(terms) => terms.len(),
    };
    // SAFETY: The bounded result has at most four alternatives. PG owns the
    // zero-terminated array and strings. Each variant shares the input position.
    unsafe {
        let result = pg_sys::palloc0(std::mem::size_of::<pg_sys::TSLexeme>() * (count + 1))
            .cast::<pg_sys::TSLexeme>();
        match outcome {
            LexizeOutcome::Terms(terms) => {
                for (index, term) in terms.into_iter().enumerate() {
                    let entry = result.add(index);
                    (*entry).lexeme = pg_string(&term);
                    (*entry).nvariant = index as u16;
                }
            }
            LexizeOutcome::Filter(term) => {
                (*result).lexeme = pg_string(&term);
                (*result).flags = pg_sys::TSL_FILTER as u16;
            }
            LexizeOutcome::Stop => {}
            LexizeOutcome::Ignore => unreachable!(),
        }
        pointer(result)
    }
}

pub(crate) fn structured_lexemes(
    terms: Option<crate::structured_tokens::StructuredTokenTerms>,
) -> Internal {
    let Some(terms) = terms else {
        return datum(0);
    };
    let count = terms.len();
    let (full, components) = terms.into_groups();
    let component_variant = full.len() as u16;
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("structured_emit");
    // SAFETY: StructuredTokenTerms validates <=66 entries and its complete byte budget
    // before any PG allocation. All returned storage belongs to PG, not Rust.
    unsafe {
        let result = pg_sys::palloc0(std::mem::size_of::<pg_sys::TSLexeme>() * (count + 1))
            .cast::<pg_sys::TSLexeme>();
        let mut index = 0;
        for (variant, term) in full.into_iter().enumerate() {
            (*result.add(index)).lexeme = pg_string(&term);
            (*result.add(index)).nvariant = variant as u16;
            index += 1;
            #[cfg(feature = "test-hooks")]
            if index == 1 {
                crate::test_hooks::hit("structured_emit_string");
            }
        }
        for term in components {
            (*result.add(index)).lexeme = pg_string(&term);
            (*result.add(index)).nvariant = component_variant;
            index += 1;
        }
        pointer(result)
    }
}

pub fn lextypes() -> Internal {
    // SAFETY: Each descriptor/string and the zero terminator are PG-owned.
    unsafe {
        let result = pg_sys::palloc0(std::mem::size_of::<pg_sys::LexDescr>() * (TYPE_COUNT + 1))
            .cast::<pg_sys::LexDescr>();
        for index in 0..TYPE_COUNT {
            let route = TokenType::from_id((index + 1) as i32).expect("fixed token type");
            let entry = result.add(index);
            (*entry).lexid = route.id();
            (*entry).alias = pg_string(route.alias());
            (*entry).descr = pg_string(if !route.is_content() {
                "Unindexed original text fragment"
            } else {
                route.alias()
            });
        }
        pointer(result)
    }
}

/// # Safety
/// INIT supplies a live PG-owned List<DefElem>; option bindings guard PG ERROR.
pub(crate) unsafe fn structured_options(options: Internal) -> crate::structured_tokens::Options {
    require_utf8();
    let mut kind = None;
    let (mut discard, mut stem, mut normalize) = (true, true, true);
    let mut seen = std::collections::BTreeSet::new();
    pgrx::memcx::current_context(|cx| {
        let list =
            unsafe { pgrx::list::List::<*mut c_void>::downcast_ptr_in_memcx(raw(options), cx) }
                .unwrap_or_else(|| invalid("invalid symbol dictionary option list"));
        for pointer in list.iter() {
            let def = (*pointer).cast::<pg_sys::DefElem>();
            let name = unsafe { CStr::from_ptr((*def).defname) }
                .to_str()
                .unwrap_or_else(|_| invalid("invalid option name UTF8"))
                .to_ascii_lowercase();
            if !seen.insert(name.clone()) {
                invalid(format!("duplicate symbol dictionary option: {name}"));
            }
            match name.as_str() {
                "kind" => {
                    let value = unsafe { CStr::from_ptr(pg_sys::defGetString(def)) }
                        .to_str()
                        .unwrap_or_else(|_| invalid("invalid Kind UTF8"));
                    kind = Some(
                        crate::structured_tokens::StructuredTokenKind::from_alias(value)
                            .unwrap_or_else(|| {
                                invalid("Kind must be url, host, email, file or numword")
                            }),
                    );
                }
                "discard" => discard = unsafe { pg_sys::defGetBoolean(def) },
                "stem" => stem = unsafe { pg_sys::defGetBoolean(def) },
                "normalize" => normalize = unsafe { pg_sys::defGetBoolean(def) },
                _ => invalid(format!("unknown symbol dictionary option: {name}")),
            }
        }
    });
    checked(crate::structured_tokens::Options::new(
        kind.unwrap_or_else(|| invalid("Kind is required")),
        discard,
        stem,
        normalize,
    ))
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
/// `options` is the INIT callback's live PG-owned List<DefElem>.
pub unsafe fn linguistic_options(options: Internal) -> crate::analysis::Options {
    require_utf8();
    let mut language = None;
    let mut discard = None;
    let mut stem = None;
    let mut english = None;
    let mut normalize = true;
    let mut filter = false;
    let mut seen = std::collections::BTreeSet::new();
    pgrx::memcx::current_context(|cx| {
        // SAFETY: INIT receives a List<DefElem> or NIL, valid for this callback.
        let list =
            unsafe { pgrx::list::List::<*mut c_void>::downcast_ptr_in_memcx(raw(options), cx) }
                .unwrap_or_else(|| invalid("invalid dictionary option list"));
        for pointer in list.iter() {
            let def = (*pointer).cast::<pg_sys::DefElem>();
            // SAFETY: All nodes/strings are PG-owned; conversion bindings guard ERROR.
            let name = unsafe { CStr::from_ptr((*def).defname) }
                .to_str()
                .unwrap_or_else(|_| invalid("invalid option name UTF8"))
                .to_ascii_lowercase();
            if !seen.insert(name.clone()) {
                invalid(format!("duplicate dictionary option: {name}"));
            }
            match name.as_str() {
                "language" => {
                    let value = unsafe { CStr::from_ptr(pg_sys::defGetString(def)) }
                        .to_str()
                        .unwrap_or_else(|_| invalid("invalid Language UTF8"));
                    language = Some(LanguageId::from_alias(value).unwrap_or_else(|| {
                        invalid("Language must be a supported lowercase ISO639-3 code or und")
                    }));
                }
                "discard" => discard = Some(unsafe { pg_sys::defGetBoolean(def) }),
                "stem" => stem = Some(unsafe { pg_sys::defGetBoolean(def) }),
                "english" => english = Some(unsafe { pg_sys::defGetBoolean(def) }),
                "normalize" => normalize = unsafe { pg_sys::defGetBoolean(def) },
                "filter" => filter = unsafe { pg_sys::defGetBoolean(def) },
                _ => invalid(format!("unknown dictionary option: {name}")),
            }
        }
    });
    let language = language.unwrap_or_else(|| invalid("Language is required"));
    let discard = discard.unwrap_or_else(|| language.has_stopwords());
    let english = english.unwrap_or_else(|| language.default_english(filter));
    if english && (!language.english_eligible() || filter) {
        invalid("English=true requires a non-English Latin language and Filter=false");
    }
    let stem = stem.unwrap_or_else(|| language.default_stem(english));
    if discard && !language.has_stopwords() {
        fail(
            PgSqlErrorCode::ERRCODE_FEATURE_NOT_SUPPORTED,
            "Discard=true requires a language with a builtin stoplist",
        );
    }
    if stem && language.algorithm().is_none() && !english {
        fail(
            PgSqlErrorCode::ERRCODE_FEATURE_NOT_SUPPORTED,
            "Stem=true requires an enabled branch with a supported stemmer",
        );
    }
    if filter && stem {
        invalid(
            "Filter=true requires Stem=false; filtering cannot preserve surface and stem alternatives",
        );
    }
    checked(crate::analysis::Options::new(
        language, discard, stem, normalize, filter, english,
    ))
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
        let token_type = TokenType::from_id(entry.type_() as i32).unwrap_or_else(|| {
            fail(
                PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                "invalid headline token type",
            )
        });
        let word = entry.repeated() == 0 && token_type.is_content();
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

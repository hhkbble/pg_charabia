use std::{
    ffi::{CStr, c_char, c_void},
    ptr, slice, str,
};

use pgrx::{AnyElement, Internal, PgLogLevel, PgSqlErrorCode, ereport, pg_sys};

use crate::analysis::{AnalysisError, ErrorKind, LexizeOutcome, MAX_INPUT_BYTES};
use crate::profile::NormalizationRoute;

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

    let mut owner = Box::new(value);
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
        (*callback).arg = ptr::from_mut(owner.as_mut()).cast();
    }
    #[cfg(feature = "test-hooks")]
    crate::test_hooks::hit("register_link");
    // SAFETY: PG18 registration only links the initialized callback; it cannot
    // ERROR. No fallible operation is allowed before the immediate handoff.
    unsafe { pg_sys::MemoryContextRegisterResetCallback(context, callback) };
    let state = Box::into_raw(owner);
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
    let term = match outcome {
        LexizeOutcome::Ignore => return datum(0),
        LexizeOutcome::Stop => None,
        LexizeOutcome::Term(term) => Some(term),
    };
    // SAFETY: Zero-filled terminators are valid TSLexeme entries. PG owns and
    // frees both this array and the separately palloc'd lexeme, if present.
    unsafe {
        let result =
            pg_sys::palloc0(std::mem::size_of::<pg_sys::TSLexeme>() * 2).cast::<pg_sys::TSLexeme>();
        if let Some(term) = term {
            (*result).lexeme = pg_string(&term);
        }
        pointer(result)
    }
}

pub fn lextypes() -> Internal {
    // SAFETY: The zeroed final LexDescr has lexid=0; all other fields and their
    // strings are PG-owned because ts_token_type frees them individually.
    unsafe {
        let result = pg_sys::palloc0(std::mem::size_of::<pg_sys::LexDescr>() * 19)
            .cast::<pg_sys::LexDescr>();
        for (index, route) in NormalizationRoute::ALL.into_iter().enumerate() {
            let entry = result.add(index);
            (*entry).lexid = route.id();
            (*entry).alias = pg_string(route.alias());
            (*entry).descr = pg_string(&format!(
                "Charabia v1 normalization context: {}",
                route.alias()
            ));
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

/// # Safety
/// `options` is the PG-owned List<DefElem> supplied to dictionary INIT.
pub unsafe fn dictionary_options(
    options: Internal,
) -> (NormalizationRoute, crate::analysis::Options) {
    require_utf8();
    let mut route = None;
    let mut lossy = None;
    let mut stopwords = None;
    pgrx::memcx::current_context(|cx| {
        // SAFETY: INIT's documented argument is a List of DefElem pointers.
        let list =
            unsafe { pgrx::list::List::<*mut c_void>::downcast_ptr_in_memcx(raw(options), cx) }
                .unwrap_or_else(|| {
                    fail(
                        PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
                        "invalid dictionary option list",
                    )
                });
        for pointer in list.iter() {
            let def = (*pointer).cast::<pg_sys::DefElem>();
            // SAFETY: Each element is a live DefElem; PG stores NUL-terminated
            // option names/values and defGet* uses pgrx's guarded PG bindings.
            let name = unsafe { CStr::from_ptr((*def).defname) }
                .to_str()
                .unwrap_or_else(|_| {
                    fail(
                        PgSqlErrorCode::ERRCODE_CHARACTER_NOT_IN_REPERTOIRE,
                        "invalid option name",
                    )
                })
                .to_ascii_lowercase();
            let name = name.as_str();
            let repeated = match name {
                "route" => route.is_some(),
                "lossy" => lossy.is_some(),
                "stopwords" => stopwords.is_some(),
                _ => fail(
                    PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
                    format!("unknown dictionary option: {name}"),
                ),
            };
            if repeated {
                fail(
                    PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
                    format!("duplicate dictionary option: {name}"),
                );
            }
            match name {
                "lossy" => lossy = Some(unsafe { pg_sys::defGetBoolean(def) }),
                "route" | "stopwords" => {
                    let value = unsafe { CStr::from_ptr(pg_sys::defGetString(def)) }
                        .to_str()
                        .unwrap_or_else(|_| {
                            fail(
                                PgSqlErrorCode::ERRCODE_CHARACTER_NOT_IN_REPERTOIRE,
                                "invalid UTF8 dictionary option",
                            )
                        });
                    if name == "route" {
                        route = Some(NormalizationRoute::from_alias(value).unwrap_or_else(|| {
                            fail(
                                PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
                                "unknown normalization Route",
                            )
                        }));
                    } else {
                        if value.len() > crate::analysis::MAX_STOP_WORDS_JSON_BYTES {
                            fail(
                                PgSqlErrorCode::ERRCODE_PROGRAM_LIMIT_EXCEEDED,
                                "StopWords JSON exceeds 1048576 bytes",
                            );
                        }
                        stopwords = Some(value.to_owned());
                    }
                }
                _ => unreachable!(),
            }
        }
    });
    let route = route.unwrap_or_else(|| {
        fail(
            PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
            "Route is required",
        )
    });
    let options = checked(crate::analysis::Options::from_json(
        lossy.unwrap_or(true),
        stopwords.as_deref(),
    ));
    (route, options)
}

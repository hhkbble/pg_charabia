//! Supplemental backend-local instrumentation, absent from release builds.
use crate::pg_ffi;
use pgrx::{JsonB, PgSqlErrorCode, pg_extern, pg_sys};
use std::{
    sync::atomic::{AtomicU32, AtomicU64, Ordering},
    thread,
    time::Duration,
};

static FAULT: AtomicU32 = AtomicU32::new(0);
static PARSER_CREATED: AtomicU64 = AtomicU64::new(0);
static PARSER_DROPPED: AtomicU64 = AtomicU64::new(0);
static NESTED_LINGUISTIC: AtomicU64 = AtomicU64::new(0);
static NESTED_STRUCTURED: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_CREATED: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_DROPPED: AtomicU64 = AtomicU64::new(0);
static STATE_REGISTERED: AtomicU64 = AtomicU64::new(0);
static STATE_RELEASED: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_LIVE_FST_BYTES: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_LAST_LEXIZED: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_LAST_CREATED_FST_BYTES: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_LIVE_REFERENCED_FST_BYTES: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_LAST_REFERENCED_FST_BYTES: AtomicU64 = AtomicU64::new(0);

pub fn parser_created() {
    PARSER_CREATED.fetch_add(1, Ordering::Relaxed);
}
pub fn parser_dropped() {
    PARSER_DROPPED.fetch_add(1, Ordering::Relaxed);
}
pub fn dictionary_created(bytes: usize, referenced: usize) -> u64 {
    DICTIONARY_LAST_REFERENCED_FST_BYTES.store(referenced as u64, Ordering::Relaxed);
    DICTIONARY_LIVE_REFERENCED_FST_BYTES.fetch_add(referenced as u64, Ordering::Relaxed);
    DICTIONARY_LAST_CREATED_FST_BYTES.store(bytes as u64, Ordering::Relaxed);
    DICTIONARY_LIVE_FST_BYTES.fetch_add(bytes as u64, Ordering::Relaxed);
    DICTIONARY_CREATED.fetch_add(1, Ordering::Relaxed) + 1
}
pub fn dictionary_dropped(bytes: usize, referenced: usize) {
    DICTIONARY_LIVE_REFERENCED_FST_BYTES.fetch_sub(referenced as u64, Ordering::Relaxed);
    DICTIONARY_DROPPED.fetch_add(1, Ordering::Relaxed);
    DICTIONARY_LIVE_FST_BYTES.fetch_sub(bytes as u64, Ordering::Relaxed);
}
pub fn dictionary_lexized(generation: u64) {
    DICTIONARY_LAST_LEXIZED.store(generation, Ordering::Relaxed);
}
pub fn state_registered() {
    STATE_REGISTERED.fetch_add(1, Ordering::Relaxed);
}
pub fn state_released() {
    STATE_RELEASED.fetch_add(1, Ordering::Relaxed);
}

fn point_id(point: &str) -> u32 {
    match point {
        "start" => 1,
        "gettoken" => 2,
        "end" => 3,
        "init" => 4,
        "lexize" => 5,
        "register_alloc" => 6,
        "register_link" => 7,
        "headline_selector" => 8,
        "headline_escape" => 9,
        "structured_emit" => 10,
        "structured_emit_string" => 11,
        _ => pg_ffi::fail(
            PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
            "unknown test fault point",
        ),
    }
}

pub fn hit(point: &str) {
    let point = point_id(point);
    let fault = FAULT.load(Ordering::Relaxed);
    if fault >> 4 != point
        || FAULT
            .compare_exchange(fault, 0, Ordering::Relaxed, Ordering::Relaxed)
            .is_err()
    {
        return;
    }
    match fault & 15 {
        1 => pg_ffi::fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "injected pg_charabia ERROR",
        ),
        2 => panic!("injected pg_charabia panic"),
        3 => {
            // SAFETY: This is a guarded backend call with a static C string.
            // The observer waits for this marker before cancelling this PID.
            unsafe { pg_sys::pgstat_report_appname(c"pgcb_hook_wait".as_ptr()) };
            for _ in 0..3000 {
                pgrx::check_for_interrupts!();
                thread::sleep(Duration::from_millis(10));
            }
            pg_ffi::fail(
                PgSqlErrorCode::ERRCODE_QUERY_CANCELED,
                "test cancellation was not delivered",
            );
        }
        4 => {
            // SAFETY: The impossible request is rejected by the real PG
            // allocator before malloc; it does not exhaust machine memory.
            unsafe { pg_sys::palloc(usize::MAX) };
            unreachable!("PostgreSQL accepted an impossible allocation");
        }
        5 | 6 => {
            // GETTOKEN invokes this before borrowing ParserState. Clearing FAULT
            // above makes the SPI parser nonrecursive while the outer state is live.
            let created = PARSER_CREATED.load(Ordering::Relaxed);
            let dropped = PARSER_DROPPED.load(Ordering::Relaxed);
            assert!(
                created > dropped,
                "nested probe requires a live outer parser"
            );
            let structured = fault & 15 == 6;
            let query = if structured {
                "SELECT to_tsvector('public.charabia_structured','100ms') @@ 'ms'::tsquery"
            } else {
                "SELECT to_tsvector('public.charabia_linguistic','100ms') @@ 'ms'::tsquery"
            };
            let actual = pgrx::Spi::get_one::<bool>(query).expect("nested parser SPI");
            assert_eq!(actual, Some(structured), "nested mode component contract");
            assert_eq!(PARSER_CREATED.load(Ordering::Relaxed), created + 1);
            assert_eq!(PARSER_DROPPED.load(Ordering::Relaxed), dropped + 1);
            if structured {
                NESTED_STRUCTURED.fetch_add(1, Ordering::Relaxed);
            } else {
                NESTED_LINGUISTIC.fetch_add(1, Ordering::Relaxed);
            }
        }
        _ => unreachable!(),
    }
}

#[pg_extern(sql = false)]
fn test_set_fault(point: &str, mode: &str) {
    #[cfg(not(test))]
    if point == "allocator" {
        allocator_probes::run(mode);
        return;
    }
    let mode = match mode {
        "error" => 1,
        "panic" => 2,
        "wait" => 3,
        "allocation_error" => 4,
        "nested_linguistic" if point == "gettoken" => 5,
        "nested_structured" if point == "gettoken" => 6,
        _ => pg_ffi::fail(
            PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
            "unknown test fault mode",
        ),
    };
    FAULT.store((point_id(point) << 4) | mode, Ordering::Relaxed);
}

#[pg_extern(sql = false)]
fn test_stats() -> JsonB {
    #[cfg(not(test))]
    let allocator = allocator_probes::stats();
    #[cfg(test)]
    let allocator = serde_json::Value::Null;
    JsonB(serde_json::json!({
        "allocator":allocator,
        "parser_created":PARSER_CREATED.load(Ordering::Relaxed),
        "parser_dropped":PARSER_DROPPED.load(Ordering::Relaxed),
        "nested_linguistic":NESTED_LINGUISTIC.load(Ordering::Relaxed),
        "nested_structured":NESTED_STRUCTURED.load(Ordering::Relaxed),
        "dictionary_created":DICTIONARY_CREATED.load(Ordering::Relaxed),
        "dictionary_dropped":DICTIONARY_DROPPED.load(Ordering::Relaxed),
        "state_registered":STATE_REGISTERED.load(Ordering::Relaxed),
        "state_released":STATE_RELEASED.load(Ordering::Relaxed),
        "dictionary_live_fst_bytes":DICTIONARY_LIVE_FST_BYTES.load(Ordering::Relaxed),
        "dictionary_last_created":DICTIONARY_CREATED.load(Ordering::Relaxed),
        "dictionary_last_lexized":DICTIONARY_LAST_LEXIZED.load(Ordering::Relaxed),
        "dictionary_last_created_fst_bytes":DICTIONARY_LAST_CREATED_FST_BYTES.load(Ordering::Relaxed),
        "dictionary_live_referenced_fst_bytes":DICTIONARY_LIVE_REFERENCED_FST_BYTES.load(Ordering::Relaxed),
        "dictionary_last_referenced_fst_bytes":DICTIONARY_LAST_REFERENCED_FST_BYTES.load(Ordering::Relaxed),
        "embedded_stop_fst_bytes":crate::resources::STOP_DATA.iter().flatten().map(|data|data.bytes.len()).sum::<usize>(),
        "embedded_english_fst_bytes":crate::resources::ENGLISH_BYTES.len(),
    }))
}

#[cfg(not(test))]
mod allocator_probes {
    use crate::{allocator::PgGlobal, pg_ffi};
    use pgrx::pg_sys;
    use std::{
        alloc::{GlobalAlloc, Layout},
        cell::RefCell,
        ptr,
        sync::atomic::{AtomicUsize, Ordering::Relaxed},
    };

    static LAYOUTS: AtomicUsize = AtomicUsize::new(0);
    static RESET_DROPS: AtomicUsize = AtomicUsize::new(0);
    static SMALL_SHRINK_REUSED: AtomicUsize = AtomicUsize::new(0);

    struct ResetProbe(Box<[u8; 32]>);
    impl Drop for ResetProbe {
        fn drop(&mut self) {
            RESET_DROPS.fetch_add(1, Relaxed);
        }
    }

    struct ExitProbe(Option<Box<[u8; 64]>>);
    impl Drop for ExitProbe {
        fn drop(&mut self) {
            drop(self.0.take());
            // The marker follows Box deallocation. Do not use PG logging,
            // formatting, or allocation during TLS teardown.
            const MARKER: &[u8] = b"PGCB_ALLOCATOR_TLS_RELEASED\n";
            unsafe extern "C" {
                fn write(fd: std::ffi::c_int, data: *const u8, size: usize) -> isize;
            }
            // SAFETY: A fixed live byte slice is written to backend stderr.
            unsafe { write(2, MARKER.as_ptr(), MARKER.len()) };
        }
    }
    std::thread_local! {
        static EXIT_PROBE: RefCell<Option<ExitProbe>> = const { RefCell::new(None) };
    }

    pub(super) fn run(mode: &str) {
        match mode {
            "contracts" => contracts(),
            "tls_exit" => EXIT_PROBE.with(|slot| {
                assert!(slot.borrow().is_none(), "TLS probe must be armed once");
                let payload = Box::new([0x5a; 64]);
                // SAFETY: This live nonzero Box must use the real global PG
                // entry, not just the direct allocator contract probe.
                unsafe {
                    let top = pg_sys::TopMemoryContext;
                    assert_eq!(
                        pg_sys::GetMemoryChunkContext(payload.as_ptr().cast_mut().cast()),
                        top
                    );
                }
                *slot.borrow_mut() = Some(ExitProbe(Some(payload)));
            }),
            _ => pg_ffi::fail(
                pgrx::PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
                "unknown allocator probe",
            ),
        }
    }

    fn contracts() {
        // Call the actual allocator interface. These are independent, fixed
        // cases rather than allocation counts inferred from optimized Boxes.
        let top = unsafe { pg_sys::TopMemoryContext };
        let mut layouts = 0;
        for size in [1, 31, 4096, 8193, 65537] {
            for align in [1, 8, 16, 64, 4096] {
                let layout = Layout::from_size_align(size, align).unwrap();
                // SAFETY: Every layout is valid and nonzero. Each successful
                // allocation is checked before access and released once.
                unsafe {
                    let p = PgGlobal.alloc(layout);
                    assert!(!p.is_null());
                    assert_eq!(p.addr() % align, 0);
                    assert_eq!(pg_sys::GetMemoryChunkContext(p.cast()), top);
                    ptr::write_bytes(p, 0x5a, size);
                    assert!(
                        std::slice::from_raw_parts(p, size)
                            .iter()
                            .all(|b| *b == 0x5a)
                    );
                    PgGlobal.dealloc(p, layout);
                    let p = PgGlobal.alloc_zeroed(layout);
                    assert!(!p.is_null());
                    assert!(std::slice::from_raw_parts(p, size).iter().all(|b| *b == 0));
                    ptr::write_bytes(p, 0xa5, size);
                    let larger = size + 19;
                    let p = PgGlobal.realloc(p, layout, larger);
                    assert!(!p.is_null());
                    assert_eq!(p.addr() % align, 0);
                    assert!(
                        std::slice::from_raw_parts(p, size)
                            .iter()
                            .all(|b| *b == 0xa5)
                    );
                    assert_eq!(pg_sys::GetMemoryChunkContext(p.cast()), top);
                    ptr::write_bytes(p, 0x3c, larger);
                    let smaller = (size / 2).max(1);
                    let p = PgGlobal.realloc(
                        p,
                        Layout::from_size_align(larger, align).unwrap(),
                        smaller,
                    );
                    assert!(!p.is_null());
                    assert_eq!(p.addr() % align, 0);
                    assert_eq!(pg_sys::GetMemoryChunkContext(p.cast()), top);
                    assert!(
                        std::slice::from_raw_parts(p, smaller)
                            .iter()
                            .all(|b| *b == 0x3c)
                    );
                    PgGlobal.dealloc(p, Layout::from_size_align(smaller, align).unwrap());
                }
                layouts += 1;
            }
        }
        // SAFETY: The rejected layouts remain valid Rust layouts. Rejection
        // happens before PG allocation and does not simulate malloc OOM.
        unsafe {
            assert!(
                PgGlobal
                    .alloc(Layout::from_size_align(1, 1 << 27).unwrap())
                    .is_null()
            );
            for align in [8, 64, 4096] {
                let huge = isize::MAX as usize - (align - 1);
                let rejected = Layout::from_size_align(huge, align).unwrap();
                assert!(PgGlobal.alloc(rejected).is_null());
                let layout = Layout::from_size_align(32, align).unwrap();
                let p = PgGlobal.alloc(layout);
                assert!(!p.is_null());
                ptr::write_bytes(p, 0x7b, 32);
                assert!(PgGlobal.realloc(p, layout, huge).is_null());
                assert!(std::slice::from_raw_parts(p, 32).iter().all(|b| *b == 0x7b));
                PgGlobal.dealloc(p, layout);
            }
            // PG18 Top AllocSet keeps an ordinary small chunk when shrinking.
            // Growth and aligned chunks have no in-place requirement.
            let layout = Layout::from_size_align(256, 8).unwrap();
            let p = PgGlobal.alloc(layout);
            assert!(!p.is_null());
            ptr::write_bytes(p, 0x42, 256);
            let address = p.addr();
            let p = PgGlobal.realloc(p, layout, 32);
            assert!(!p.is_null());
            assert_eq!(p.addr(), address);
            assert_eq!(pg_sys::GetMemoryChunkContext(p.cast()), top);
            assert!(std::slice::from_raw_parts(p, 32).iter().all(|b| *b == 0x42));
            PgGlobal.dealloc(p, Layout::from_size_align(32, 8).unwrap());
            SMALL_SHRINK_REUSED.store(1, Relaxed);
        }

        let mut context = pgrx::PgMemoryContexts::new("pgcb allocator contracts");
        let layout = Layout::from_size_align(32, 8).unwrap();
        let before = RESET_DROPS.load(Relaxed);
        // SAFETY: The private context is live. No borrowed data escapes it.
        // Global allocations belong to Top, including owned_state's Box.
        let p = unsafe {
            context.switch_to(|_| {
                let p = PgGlobal.alloc(layout);
                assert!(!p.is_null());
                ptr::write_bytes(p, 0x69, 32);
                let owner = ResetProbe(Box::new([0x5a; 32]));
                assert_eq!(
                    pg_sys::GetMemoryChunkContext(owner.0.as_ptr().cast_mut().cast()),
                    top
                );
                let state = pg_ffi::owned_state(owner);
                assert_eq!(
                    pg_sys::GetMemoryChunkContext(pg_ffi::raw::<ResetProbe>(state).cast()),
                    top
                );
                p
            })
        };
        // SAFETY: The context owns only the reset callback, while both Rust
        // owners and the separately retained raw block use Top storage.
        unsafe {
            context.reset();
            assert_eq!(RESET_DROPS.load(Relaxed) - before, 1);
            assert!(std::slice::from_raw_parts(p, 32).iter().all(|b| *b == 0x69));
            assert_eq!(pg_sys::GetMemoryChunkContext(p.cast()), top);
            PgGlobal.dealloc(p, layout);
            context.reset();
        }
        assert_eq!(RESET_DROPS.load(Relaxed) - before, 1);
        LAYOUTS.store(layouts, Relaxed);
    }

    pub(super) fn stats() -> serde_json::Value {
        let (calls, first_top) = crate::allocator::diagnostics();
        serde_json::json!({
            "contract_layouts":LAYOUTS.load(Relaxed),
            "reset_drops":RESET_DROPS.load(Relaxed),
            "small_shrink_reused":SMALL_SHRINK_REUSED.load(Relaxed) == 1,
            "alloc_calls":calls,
            "first_alloc_top":first_top != 0 && first_top == unsafe { pg_sys::TopMemoryContext } as usize,
        })
    }
}

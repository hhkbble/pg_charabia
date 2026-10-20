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
static DICTIONARY_CREATED: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_DROPPED: AtomicU64 = AtomicU64::new(0);
static STATE_REGISTERED: AtomicU64 = AtomicU64::new(0);
static STATE_RELEASED: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_LIVE_FST_BYTES: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_LAST_LEXIZED: AtomicU64 = AtomicU64::new(0);
static DICTIONARY_LAST_CREATED_FST_BYTES: AtomicU64 = AtomicU64::new(0);

pub fn parser_created() {
    PARSER_CREATED.fetch_add(1, Ordering::Relaxed);
}
pub fn parser_dropped() {
    PARSER_DROPPED.fetch_add(1, Ordering::Relaxed);
}
pub fn dictionary_created(bytes: usize) -> u64 {
    DICTIONARY_LAST_CREATED_FST_BYTES.store(bytes as u64, Ordering::Relaxed);
    DICTIONARY_LIVE_FST_BYTES.fetch_add(bytes as u64, Ordering::Relaxed);
    DICTIONARY_CREATED.fetch_add(1, Ordering::Relaxed) + 1
}
pub fn dictionary_dropped(bytes: usize) {
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
        _ => unreachable!(),
    }
}

#[pg_extern(sql = false)]
fn test_set_fault_v1(point: &str, mode: &str) {
    let mode = match mode {
        "error" => 1,
        "panic" => 2,
        "wait" => 3,
        "allocation_error" => 4,
        _ => pg_ffi::fail(
            PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
            "unknown test fault mode",
        ),
    };
    FAULT.store((point_id(point) << 4) | mode, Ordering::Relaxed);
}

#[pg_extern(sql = false)]
fn test_stats_v1() -> JsonB {
    JsonB(serde_json::json!({
        "parser_created":PARSER_CREATED.load(Ordering::Relaxed),
        "parser_dropped":PARSER_DROPPED.load(Ordering::Relaxed),
        "dictionary_created":DICTIONARY_CREATED.load(Ordering::Relaxed),
        "dictionary_dropped":DICTIONARY_DROPPED.load(Ordering::Relaxed),
        "state_registered":STATE_REGISTERED.load(Ordering::Relaxed),
        "state_released":STATE_RELEASED.load(Ordering::Relaxed),
        "dictionary_live_fst_bytes":DICTIONARY_LIVE_FST_BYTES.load(Ordering::Relaxed),
        "dictionary_last_created":DICTIONARY_CREATED.load(Ordering::Relaxed),
        "dictionary_last_lexized":DICTIONARY_LAST_LEXIZED.load(Ordering::Relaxed),
        "dictionary_last_created_fst_bytes":DICTIONARY_LAST_CREATED_FST_BYTES.load(Ordering::Relaxed),
    }))
}

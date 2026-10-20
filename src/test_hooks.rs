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

pub fn parser_created() {
    PARSER_CREATED.fetch_add(1, Ordering::Relaxed);
}
pub fn parser_dropped() {
    PARSER_DROPPED.fetch_add(1, Ordering::Relaxed);
}
pub fn dictionary_created() {
    DICTIONARY_CREATED.fetch_add(1, Ordering::Relaxed);
}
pub fn dictionary_dropped() {
    DICTIONARY_DROPPED.fetch_add(1, Ordering::Relaxed);
}

fn point_id(point: &str) -> u32 {
    match point {
        "start" => 1,
        "gettoken" => 2,
        "end" => 3,
        "init" => 4,
        "lexize" => 5,
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
        _ => unreachable!(),
    }
}

#[pg_extern(sql = false)]
fn test_set_fault_v1(point: &str, mode: &str) {
    let mode = match mode {
        "error" => 1,
        "panic" => 2,
        "wait" => 3,
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
    }))
}

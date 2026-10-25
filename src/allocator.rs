// Rust retains ownership. PG supplies storage that outlives request contexts.
#[cfg(not(test))]
use pgrx::pg_sys;
#[cfg(not(test))]
use std::{
    alloc::{GlobalAlloc, Layout},
    ptr,
};

#[cfg(all(
    not(test),
    not(all(
        target_pointer_width = "64",
        any(target_os = "linux", target_os = "macos")
    ))
))]
compile_error!("The PG allocator requires a supported 64-bit Linux or macOS target");

const MAX_ALIGNMENT: usize = 128 * 1024 * 1024;

// PG18 aligned allocation needs at most twice the requested alignment in
// extra bytes on the supported 64-bit targets, including checking builds.
// Do not use the incorrectly generated pg_sys::MaxAllocHugeSize constant.
fn layout_supported(size: usize, align: usize) -> bool {
    align < MAX_ALIGNMENT
        && align
            .checked_mul(2)
            .and_then(|extra| size.checked_add(extra))
            .is_some_and(|total| total <= usize::MAX / 2)
}

#[cfg(not(test))]
// Callers and runtime dependencies must allocate and release only on the PG
// backend main thread. This module does not detect or support other threads.
pub(crate) struct PgGlobal;

#[cfg(all(not(test), feature = "test-hooks"))]
static ALLOC_CALLS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
#[cfg(all(not(test), feature = "test-hooks"))]
static FIRST_TOP: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

#[cfg(all(not(test), feature = "test-hooks"))]
pub(crate) fn diagnostics() -> (usize, usize) {
    use std::sync::atomic::Ordering::Relaxed;
    (ALLOC_CALLS.load(Relaxed), FIRST_TOP.load(Relaxed))
}

// SAFETY: Callers and reviewed runtime dependencies must stay on the backend
// main thread. A pgrx thread-check panic must never unwind out of this trait.
// Calls must not reenter, raise PG ERROR, or unwind. They have a live Top
// AllocSet outside critical sections, including during dependency destruction.
// This module never resets or deletes Top and does not check these contracts.
// Unexpected PG error conversion can allocate recursively without an active
// abort here. Checked layouts and NO_OOM/HUGE exclude known allocation ERROR
// paths. Valid owned pointers allow native free and
// realloc dispatch. NO_OOM preserves the original block if realloc fails.
// The guarded success paths do not allocate or unwind. No SQL, interrupt check,
// cache invalidation, callback, context reset, or error reporting runs here.
#[cfg(not(test))]
unsafe impl GlobalAlloc for PgGlobal {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if !layout_supported(layout.size(), layout.align()) {
            return ptr::null_mut();
        }
        // SAFETY: The caller guarantees the live backend Top described above.
        let top = unsafe { pg_sys::TopMemoryContext };
        #[cfg(feature = "test-hooks")]
        if ALLOC_CALLS.fetch_add(1, std::sync::atomic::Ordering::Relaxed) == 0 {
            FIRST_TOP.store(top as usize, std::sync::atomic::Ordering::Relaxed);
        }
        // SAFETY: Caller contracts and checked layouts establish the invariants.
        unsafe {
            pg_sys::MemoryContextAllocAligned(
                top,
                layout.size(),
                layout.align(),
                (pg_sys::MCXT_ALLOC_NO_OOM | pg_sys::MCXT_ALLOC_HUGE) as i32,
            )
            .cast()
        }
    }

    unsafe fn dealloc(&self, pointer: *mut u8, _layout: Layout) {
        // SAFETY: GlobalAlloc requires a live pointer from this allocator.
        // PG also accepts its own aligned-allocation redirection pointers.
        unsafe { pg_sys::pfree(pointer.cast()) };
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if !layout_supported(new_size, layout.align()) {
            return ptr::null_mut();
        }
        // SAFETY: The caller supplies a live allocation and a valid new
        // size for its original alignment. PG preserves that alignment,
        // including aligned redirection chunks. A null result retains
        // the old block. Only the returned pointer is used on success.
        unsafe {
            pg_sys::repalloc_extended(
                pointer.cast(),
                new_size,
                (pg_sys::MCXT_ALLOC_NO_OOM | pg_sys::MCXT_ALLOC_HUGE) as i32,
            )
            .cast()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{MAX_ALIGNMENT, layout_supported};
    use std::alloc::Layout;

    #[test]
    fn supports_normal_and_high_alignment() {
        for size in [1, 31, 4096, 8193, 65537] {
            for align in [1, 8, 16, 64, 4096, MAX_ALIGNMENT / 2] {
                let layout = Layout::from_size_align(size, align).unwrap();
                assert!(layout_supported(layout.size(), layout.align()));
            }
        }
    }

    #[test]
    fn rejects_valid_layouts_outside_pg_limits() {
        let alignment = Layout::from_size_align(1, MAX_ALIGNMENT).unwrap();
        assert!(!layout_supported(alignment.size(), alignment.align()));
        let huge = Layout::from_size_align(isize::MAX as usize - 7, 8).unwrap();
        assert!(!layout_supported(huge.size(), huge.align()));
    }

    #[test]
    fn checks_budget_and_arithmetic_overflow_without_allocating() {
        let limit = usize::MAX / 2;
        assert!(layout_supported(limit - 16, 8));
        assert!(!layout_supported(limit - 15, 8));
        assert!(!layout_supported(usize::MAX, 8));
        assert!(!layout_supported(1, usize::MAX));
    }
}

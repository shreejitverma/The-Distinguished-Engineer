//! Retired-instruction and cycle counters for the current process.
//!
//! macOS exposes both without privileges through `proc_pid_rusage(2)` with the
//! `RUSAGE_INFO_V4` flavor (`ri_instructions`, `ri_cycles` in `<sys/resource.h>`).
//! Retired instructions do not depend on which core ran the thread or how warm the caches
//! were, so they are the one runtime number in this lab that is stable enough to record.
//! Cycles do depend on both, so the lab only ever prints them on stderr.

/// A snapshot of the two counters for the whole process, all threads included.
#[derive(Clone, Copy, Debug)]
pub struct Snapshot {
    pub instructions: u64,
    pub cycles: u64,
}

#[cfg(target_os = "macos")]
mod imp {
    use super::Snapshot;

    /// `struct rusage_info_v4` from the macOS SDK's `<sys/resource.h>`, field for field.
    #[repr(C)]
    #[derive(Default)]
    struct RusageInfoV4 {
        ri_uuid: [u8; 16],
        ri_user_time: u64,
        ri_system_time: u64,
        ri_pkg_idle_wkups: u64,
        ri_interrupt_wkups: u64,
        ri_pageins: u64,
        ri_wired_size: u64,
        ri_resident_size: u64,
        ri_phys_footprint: u64,
        ri_proc_start_abstime: u64,
        ri_proc_exit_abstime: u64,
        ri_child_user_time: u64,
        ri_child_system_time: u64,
        ri_child_pkg_idle_wkups: u64,
        ri_child_interrupt_wkups: u64,
        ri_child_pageins: u64,
        ri_child_elapsed_abstime: u64,
        ri_diskio_bytesread: u64,
        ri_diskio_byteswritten: u64,
        ri_cpu_time_qos_default: u64,
        ri_cpu_time_qos_maintenance: u64,
        ri_cpu_time_qos_background: u64,
        ri_cpu_time_qos_utility: u64,
        ri_cpu_time_qos_legacy: u64,
        ri_cpu_time_qos_user_initiated: u64,
        ri_cpu_time_qos_user_interactive: u64,
        ri_billed_system_time: u64,
        ri_serviced_system_time: u64,
        ri_logical_writes: u64,
        ri_lifetime_max_phys_footprint: u64,
        ri_instructions: u64,
        ri_cycles: u64,
        ri_billed_energy: u64,
        ri_serviced_energy: u64,
        ri_interval_max_phys_footprint: u64,
        ri_runnable_time: u64,
    }

    const RUSAGE_INFO_V4: i32 = 4;

    extern "C" {
        fn getpid() -> i32;
        fn proc_pid_rusage(pid: i32, flavor: i32, buffer: *mut RusageInfoV4) -> i32;
    }

    pub fn snapshot() -> Option<Snapshot> {
        let mut info = RusageInfoV4::default();
        // SAFETY: `info` is a valid, writable `rusage_info_v4`, which is what flavor 4 fills.
        let rc = unsafe { proc_pid_rusage(getpid(), RUSAGE_INFO_V4, &mut info) };
        if rc != 0 || info.ri_instructions == 0 {
            return None;
        }
        Some(Snapshot {
            instructions: info.ri_instructions,
            cycles: info.ri_cycles,
        })
    }
}

#[cfg(not(target_os = "macos"))]
mod imp {
    use super::Snapshot;

    /// Linux needs `perf_event_open(2)` for the same number, which is the subject of
    /// another note; this lab reports the counter as unavailable there.
    pub fn snapshot() -> Option<Snapshot> {
        None
    }
}

/// Reads both counters; `None` where the platform offers no unprivileged source.
pub fn snapshot() -> Option<Snapshot> {
    imp::snapshot()
}

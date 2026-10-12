//! Windows process containment and observed memory, without a renderer process API.
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    mem::size_of,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    process::Child,
};
use windows_sys::Win32::{
    Foundation::{GetLastError, ERROR_NO_MORE_FILES, INVALID_HANDLE_VALUE},
    System::{
        Diagnostics::ToolHelp::{
            CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
            TH32CS_SNAPPROCESS,
        },
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
        ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
        Threading::{GetCurrentProcess, OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ},
    },
};

pub fn contain_child(child: &Child) -> Result<OwnedHandle, &'static str> {
    // The sidecar cannot launch the worker until stdin bootstrap/request frames,
    // which are sent only after assignment to this unnamed, non-inherited job.
    unsafe {
        let raw = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if raw.is_null() {
            return Err("SIDECAR_JOB_UNAVAILABLE");
        }
        let job = OwnedHandle::from_raw_handle(raw);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            raw,
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
            || AssignProcessToJobObject(raw, child.as_raw_handle()) == 0
        {
            return Err("SIDECAR_JOB_UNAVAILABLE");
        }
        Ok(job)
    }
}

fn working_set(handle: *mut core::ffi::c_void) -> Option<u64> {
    unsafe {
        let mut counters: PROCESS_MEMORY_COUNTERS = std::mem::zeroed();
        counters.cb = size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
        if GetProcessMemoryInfo(
            handle,
            &mut counters,
            size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
        ) == 0
        {
            None
        } else {
            Some(counters.WorkingSetSize as u64)
        }
    }
}

pub fn metrics() -> Value {
    let root_pid = std::process::id();
    let main = unsafe { working_set(GetCurrentProcess()) };
    let mut processes = Vec::new();
    let mut enumeration_complete = true;
    unsafe {
        let raw = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if raw == INVALID_HANDLE_VALUE {
            return json!({"main_working_set_bytes":main,"process_tree_status":"NOT_MEASURED","process_tree":[],"webview2_working_set_bytes":null,"process_tree_working_set_bytes":null});
        }
        let snapshot = OwnedHandle::from_raw_handle(raw);
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;
        let mut found = Process32FirstW(snapshot.as_raw_handle(), &mut entry);
        if found == 0 {
            enumeration_complete = false;
        }
        while found != 0 {
            let end = entry
                .szExeFile
                .iter()
                .position(|value| *value == 0)
                .unwrap_or(entry.szExeFile.len());
            processes.push((
                entry.th32ProcessID,
                entry.th32ParentProcessID,
                String::from_utf16_lossy(&entry.szExeFile[..end]),
            ));
            if processes.len() >= 8192 {
                enumeration_complete = false;
                break;
            }
            found = Process32NextW(snapshot.as_raw_handle(), &mut entry);
            if found == 0 && GetLastError() != ERROR_NO_MORE_FILES {
                enumeration_complete = false;
            }
        }
    }
    if !processes.iter().any(|(pid, _, _)| *pid == root_pid) {
        enumeration_complete = false;
    }
    let mut descendants = BTreeSet::from([root_pid]);
    for _ in 0..64 {
        let before = descendants.len();
        for (pid, parent, _) in &processes {
            if descendants.contains(parent) {
                descendants.insert(*pid);
            }
        }
        if before == descendants.len() {
            break;
        }
    }
    let mut rows = Vec::new();
    let mut all_measured = enumeration_complete;
    let mut webview_measured = enumeration_complete;
    let mut total = 0_u64;
    let mut webview_total = 0_u64;
    let mut webview_count = 0_u64;
    for (pid, parent, name) in processes
        .into_iter()
        .filter(|(pid, _, _)| descendants.contains(pid))
    {
        let bytes = if pid == root_pid {
            main
        } else {
            unsafe {
                let raw = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, 0, pid);
                if raw.is_null() {
                    None
                } else {
                    let process = OwnedHandle::from_raw_handle(raw);
                    working_set(process.as_raw_handle())
                }
            }
        };
        let webview = name.eq_ignore_ascii_case("msedgewebview2.exe");
        if let Some(bytes) = bytes {
            total += bytes;
            if webview {
                webview_total += bytes;
            }
        } else {
            all_measured = false;
            if webview {
                webview_measured = false;
            }
        }
        if webview {
            webview_count += 1;
        }
        rows.push(json!({"pid":pid,"parent_pid":parent,"name":name,"working_set_bytes":bytes}));
    }
    rows.sort_by_key(|row| row["pid"].as_u64());
    json!({"main_working_set_bytes":main,"process_tree_status":if all_measured {"MEASURED"} else {"PARTIAL"},"process_tree":rows,"process_tree_working_set_bytes":if all_measured {Some(total)} else {None},"webview2_process_count":webview_count,"webview2_working_set_bytes":if webview_measured && webview_count > 0 {Some(webview_total)} else {None},"measurement_note":"One post-check live working-set snapshot. Includes current app descendants; exited worker is absent. This is not a peak, long-run, or clean-Windows benchmark."})
}

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(windows)]
mod desktop;
#[cfg(windows)]
mod native;
mod protocol;
mod sidecar;

#[cfg(windows)]
fn main() {
    desktop::run();
}

#[cfg(not(windows))]
fn main() {
    eprintln!("WINDOWS_REQUIRED: the comparison desktop and native DPAPI were not run.");
    std::process::exit(2);
}

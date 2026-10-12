fn main() {
    #[cfg(windows)]
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["is_smoke", "run_check", "finish_smoke"]),
        ),
    )
    .expect("TAURI_BUILD_FAILED");
}

use crate::{
    native,
    protocol::{self, Check},
    sidecar::{self, Sidecar},
};
use serde_json::{json, Map, Value};
use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::Write,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Instant,
};
use tauri::{AppHandle, Manager, State, WebviewWindow};

struct Backend {
    sidecar: Option<Sidecar>,
    startup_error: Option<&'static str>,
    observed: BTreeMap<Check, Value>,
    started: Instant,
    report_directory: Option<PathBuf>,
    finished: Option<Value>,
}

#[derive(Clone)]
struct BackendState(Arc<Mutex<Backend>>);

fn trusted(window: &WebviewWindow) -> Result<(), String> {
    let url = window.url().map_err(|_| "UNTRUSTED_WINDOW")?;
    let local = (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (matches!(url.scheme(), "http" | "https") && url.host_str() == Some("tauri.localhost"));
    if window.label() != "main"
        || !local
        || url.port().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
        || !matches!(url.path(), "/" | "/index.html")
        || url.query().is_some()
    {
        return Err("UNTRUSTED_WINDOW".into());
    }
    Ok(())
}

#[tauri::command]
fn is_smoke(window: WebviewWindow, state: State<'_, BackendState>) -> Result<bool, String> {
    trusted(&window)?;
    Ok(state
        .inner()
        .0
        .try_lock()
        .map_err(|_| "SIDECAR_BUSY")?
        .report_directory
        .is_some())
}

#[tauri::command]
async fn run_check(
    window: WebviewWindow,
    check: Check,
    state: State<'_, BackendState>,
) -> Result<Value, String> {
    trusted(&window)?;
    let backend = state.inner().0.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut backend = backend.try_lock().map_err(|_| "SIDECAR_BUSY".to_owned())?;
        if backend.finished.is_some() {
            return Err("SMOKE_ALREADY_FINISHED".to_owned());
        }
        let request_id = sidecar::request_id().map_err(str::to_owned)?;
        let response = if let Some(client) = backend.sidecar.as_mut() {
            match client.request(check, &request_id) {
                Ok(response) => response,
                Err(code) => protocol::failure(check, &request_id, code),
            }
        } else {
            protocol::failure(
                check,
                &request_id,
                backend.startup_error.unwrap_or("SIDECAR_UNAVAILABLE"),
            )
        };
        backend.observed.insert(check, response.clone());
        Ok(response)
    })
    .await
    .map_err(|_| "BACKEND_UNAVAILABLE".to_owned())?
}

#[tauri::command]
fn finish_smoke(window: WebviewWindow, state: State<'_, BackendState>) -> Result<Value, String> {
    trusted(&window)?;
    let mut backend = state.inner().0.try_lock().map_err(|_| "SIDECAR_BUSY")?;
    if let Some(report) = backend.finished.as_ref() {
        return Ok(report.clone());
    }
    let directory = backend
        .report_directory
        .as_ref()
        .ok_or("SMOKE_DISABLED")?
        .clone();
    let mut checks = Map::new();
    for check in [Check::Health, Check::WorkerProbe, Check::VaultProbe] {
        let response = backend
            .observed
            .get(&check)
            .ok_or("SMOKE_CHECKS_INCOMPLETE")?;
        let outcome = if response["ok"] == true {
            response["result"].clone()
        } else {
            json!({"status":"FAIL","code":response["error"]["code"]})
        };
        checks.insert(check.report_name().into(), outcome);
    }
    let visible = window
        .is_visible()
        .map_err(|_| "WINDOW_STATE_UNAVAILABLE")?;
    let engine_frozen = checks["health"]["frozen"] == true;
    let checks_pass = checks.values().all(|check| check["status"] == "PASS");
    let status =
        if !visible || !checks_pass || !engine_frozen || checks["health"]["platform"] != "win32" {
            "FAIL"
        } else {
            "PASS"
        };
    let mut metrics = native::metrics();
    metrics["elapsed_ms"] = json!(backend.started.elapsed().as_millis());
    let report = json!({
        "schema_version":1,
        "status":status,
        "scope":"AIK-002 independent Tauri Windows foundation comparison",
        "platform":"win32",
        "app_version":env!("CARGO_PKG_VERSION"),
        "tauri_version":tauri::VERSION,
        "webview2_version":tauri::webview_version().ok(),
        "packaged":!tauri::is_dev(),
        "window_visible":visible,
        "native_pid":std::process::id(),
        "sidecar_pid":backend.sidecar.as_ref().map(Sidecar::pid),
        "engine_frozen":engine_frozen,
        "checks":checks,
        "metrics":metrics,
        "renderer_node_access":null,
        "renderer_node_access_status":"NOT_MEASURED_BY_NATIVE_BACKEND",
        "renderer_bridge":"three typed fixed check names; no shell, argv, path, secret, or result input",
        "product_integration":"NOT_RUN",
        "windows11_clean_install":"NOT_RUN"
    });
    let temporary = directory.join("report.json.tmp");
    let destination = directory.join("report.json");
    // Never overwrite an existing run. The harness provides a new absolute directory.
    if destination.exists() {
        return Err("REPORT_ALREADY_EXISTS".into());
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|_| "REPORT_WRITE_FAILED")?;
    let bytes = serde_json::to_vec_pretty(&report).map_err(|_| "REPORT_WRITE_FAILED")?;
    file.write_all(&bytes)
        .and_then(|_| file.write_all(b"\n"))
        .and_then(|_| file.sync_all())
        .map_err(|_| "REPORT_WRITE_FAILED")?;
    drop(file);
    fs::rename(&temporary, &destination).map_err(|_| "REPORT_WRITE_FAILED")?;
    backend.finished = Some(report.clone());
    // Keep the real window open for the external Windows screenshot and process
    // snapshot. The harness closes it normally; RunEvent::Exit owns child cleanup.
    Ok(report)
}

fn close(app: &AppHandle) {
    if let Some(state) = app.try_state::<BackendState>() {
        if let Ok(mut backend) = state.inner().0.lock() {
            backend.sidecar.take();
        };
    }
}

pub fn run() {
    let started = Instant::now();
    let report_directory = std::env::var_os("AIK_COMPARISON_REPORT_DIR").map(PathBuf::from);
    if report_directory
        .as_ref()
        .is_some_and(|path| !path.is_absolute() || fs::create_dir_all(path).is_err())
    {
        eprintln!("REPORT_DIRECTORY_INVALID");
        std::process::exit(2);
    }
    let app = tauri::Builder::default()
        .setup(move |app| {
            let client = app
                .path()
                .resource_dir()
                .map_err(|_| "SIDECAR_PATH_INVALID")
                .and_then(|resource| Sidecar::bundled(&resource));
            let (sidecar, startup_error) = match client {
                Ok(client) => (Some(client), None),
                Err(code) => (None, Some(code)),
            };
            app.manage(BackendState(Arc::new(Mutex::new(Backend {
                sidecar,
                startup_error,
                observed: BTreeMap::new(),
                started,
                report_directory,
                finished: None,
            }))));
            if let Some(window) = app.get_webview_window("main") {
                window.show()?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![is_smoke, run_check, finish_smoke])
        .build(tauri::generate_context!())
        .expect("TAURI_BUILD_FAILED");
    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            close(app);
        }
    });
}

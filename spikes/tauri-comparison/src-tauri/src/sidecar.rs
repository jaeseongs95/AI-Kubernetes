use crate::protocol::{self, Check, PROTOCOL, WORKSPACE};
use serde_json::{json, Value};
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::{
    ffi::OsString,
    io::{BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const TIMEOUT: Duration = Duration::from_secs(15);
type Frame = Result<Vec<u8>, &'static str>;

pub fn request_id() -> Result<String, &'static str> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| "RANDOM_SOURCE_UNAVAILABLE")?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    ))
}

pub struct Sidecar {
    child: Child,
    stdin: Option<ChildStdin>,
    frames: Receiver<Frame>,
    reader: Option<JoinHandle<()>>,
    nonce: String,
    closed: bool,
    #[cfg(windows)]
    job: Option<std::os::windows::io::OwnedHandle>,
}

impl Sidecar {
    pub fn bundled(resource_directory: &Path) -> Result<Self, &'static str> {
        if !cfg!(windows) {
            return Err("WINDOWS_REQUIRED");
        }
        Self::start(
            &resource_directory.join("engine").join("aik-engine.exe"),
            &[],
        )
    }

    fn start(executable: &Path, arguments: &[OsString]) -> Result<Self, &'static str> {
        if !executable.is_absolute() {
            return Err("SIDECAR_PATH_INVALID");
        }
        let executable = executable
            .canonicalize()
            .map_err(|_| "SIDECAR_NOT_INSTALLED")?;
        if !executable.is_file() {
            return Err("SIDECAR_NOT_INSTALLED");
        }
        let mut random = [0_u8; 32];
        getrandom::fill(&mut random).map_err(|_| "RANDOM_SOURCE_UNAVAILABLE")?;
        let nonce: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        random.fill(0);
        let mut command = Command::new(&executable);
        command
            .args(arguments)
            .current_dir(executable.parent().ok_or("SIDECAR_PATH_INVALID")?)
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let allowed = [
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LOCALAPPDATA",
            "USERPROFILE",
            "APPDATA",
            "PATH",
            "LANG",
            "LC_ALL",
        ];
        for (key, value) in std::env::vars_os() {
            if allowed.contains(&key.to_string_lossy().to_ascii_uppercase().as_str()) {
                command.env(key, value);
            }
        }
        #[cfg(windows)]
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW; no shell is involved.
        let mut child = command.spawn().map_err(|_| "SIDECAR_START_FAILED")?;
        #[cfg(windows)]
        let job = match crate::native::contain_child(&child) {
            Ok(job) => job,
            Err(code) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(code);
            }
        };
        let (sender, frames) = mpsc::sync_channel(4);
        let stdin = child.stdin.take();
        let stdout = child.stdout.take();
        let mut client = Self {
            child,
            stdin,
            frames,
            reader: None,
            nonce,
            closed: false,
            #[cfg(windows)]
            job: Some(job),
        };
        let stdout = stdout.ok_or("SIDECAR_PIPE_UNAVAILABLE")?;
        client.reader = Some(
            thread::Builder::new()
                .name("aik-sidecar-frames".into())
                .spawn(move || {
                    let mut reader = BufReader::new(stdout);
                    loop {
                        let frame = match protocol::read_frame(&mut reader) {
                            Ok(Some(frame)) => Ok(frame),
                            Ok(None) => Err("SIDECAR_EXITED"),
                            Err(_) => Err("SIDECAR_INVALID_RESPONSE"),
                        };
                        let terminal = frame.is_err();
                        // An unsolicited producer cannot grow memory or block this reader.
                        if sender.try_send(frame).is_err() || terminal {
                            break;
                        }
                    }
                })
                .map_err(|_| "SIDECAR_READER_FAILED")?,
        );
        client
            .write(&json!({"kind":"bootstrap","protocol_version":PROTOCOL,"nonce":client.nonce}))?;
        let ready = client.read()?;
        if !protocol::validate_ready(&ready, client.child.id()) {
            return Err("SIDECAR_INVALID_RESPONSE");
        }
        Ok(client)
    }

    fn write(&mut self, value: &Value) -> Result<(), &'static str> {
        let mut bytes = serde_json::to_vec(value).map_err(|_| "INVALID_REQUEST")?;
        bytes.push(b'\n');
        if bytes.len() > protocol::MAX_FRAME {
            return Err("INVALID_REQUEST");
        }
        self.stdin
            .as_mut()
            .ok_or("SIDECAR_UNAVAILABLE")?
            .write_all(&bytes)
            .map_err(|_| "SIDECAR_PIPE_CLOSED")
    }

    fn read(&self) -> Result<Value, &'static str> {
        let bytes = self
            .frames
            .recv_timeout(TIMEOUT)
            .map_err(|error| match error {
                mpsc::RecvTimeoutError::Timeout => "SIDECAR_REQUEST_TIMEOUT",
                _ => "SIDECAR_EXITED",
            })??;
        protocol::parse(&bytes)
    }

    pub fn request(&mut self, check: Check, request_id: &str) -> Result<Value, &'static str> {
        if self.closed {
            return Err("SIDECAR_UNAVAILABLE");
        }
        let result = (|| {
            self.write(&json!({"protocol_version":PROTOCOL,"request_id":request_id,"command":check.command(),"workspace_id":WORKSPACE,"payload":{},"capability":self.nonce}))?;
            let mut value = self.read()?;
            if !protocol::validate_response(&value, request_id, check, self.child.id()) {
                return Err("SIDECAR_INVALID_RESPONSE");
            }
            if value["ok"] == false {
                value["error"]["message"] =
                    json!("요청을 처리하지 못했습니다. 오류 코드를 확인하세요.");
            }
            Ok(value)
        })();
        if result.is_err() {
            self.close();
        }
        result
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    pub fn close(&mut self) {
        if self.closed {
            return;
        }
        self.closed = true;
        self.stdin.take(); // EOF requests a normal stop before the bounded fallback.
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            match self.child.try_wait() {
                Ok(Some(_)) => break,
                _ if Instant::now() >= deadline => {
                    let _ = self.child.kill();
                    let _ = self.child.wait();
                    break;
                }
                _ => thread::sleep(Duration::from_millis(10)),
            }
        }
        #[cfg(windows)]
        self.job.take(); // KILL_ON_JOB_CLOSE also owns any in-flight synthetic worker.
                         // The nonce never crosses into the renderer, argv, environment, or report.
                         // This only clears this buffer; whole-process/OS memory erasure is not claimed.
        unsafe {
            self.nonce.as_bytes_mut().fill(0);
        }
        self.nonce.clear();
        if self
            .reader
            .as_ref()
            .is_some_and(|thread| thread.is_finished())
        {
            if let Some(thread) = self.reader.take() {
                let _ = thread.join();
            }
        }
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        self.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_random_ids_are_distinct_uuid_v4() {
        let left = request_id().unwrap();
        let right = request_id().unwrap();
        assert_ne!(left, right);
        assert_eq!(left.len(), 36);
        assert_eq!(&left[14..15], "4");
        assert!(matches!(&left[19..20], "8" | "9" | "a" | "b"));
    }

    #[test]
    fn actual_python_child_and_worker_complete_unchanged_contract() {
        // Required explicit development configuration: this test must not quietly
        // report PASS when Python was unavailable. CI sets the same variable as Electron.
        let executable = PathBuf::from(
            std::env::var_os("AIK_SPIKE_PYTHON")
                .expect("set AIK_SPIKE_PYTHON to the absolute test Python executable"),
        );
        let entry = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../windows-foundation/python/sidecar_entry.py")
            .canonicalize()
            .unwrap();
        let mut client =
            Sidecar::start(&executable, &[OsString::from("-I"), entry.into_os_string()]).unwrap();
        let pid = client.pid();
        for check in [Check::Health, Check::WorkerProbe, Check::VaultProbe] {
            let response = client.request(check, &request_id().unwrap()).unwrap();
            assert_eq!(response["ok"], true);
            assert_eq!(
                response["result"]["status"],
                if check == Check::VaultProbe && !cfg!(windows) {
                    "NOT_RUN"
                } else {
                    "PASS"
                }
            );
            if check == Check::WorkerProbe {
                assert_eq!(response["result"]["parent_pid"], pid);
            }
        }
        client.close();
        assert!(client.child.try_wait().unwrap().is_some());
    }
}

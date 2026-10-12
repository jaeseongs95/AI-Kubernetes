//! The same foundation-spike wire contract, with all caller authority kept native.
use serde::{
    de::{self, MapAccess, SeqAccess, Visitor},
    Deserialize,
};
use serde_json::{json, Map, Number, Value};
use std::{
    fmt,
    io::{self, BufRead},
};

pub const PROTOCOL: u64 = 1;
pub const WORKSPACE: &str = "foundation-spike";
pub const MAX_FRAME: usize = 65_536;
const SAMPLE_SHA256: &str = "82b2d2ffd52c4fcda74566e4950be2d32f128d466444883442aee643d34c10ee";

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum Check {
    Health,
    WorkerProbe,
    VaultProbe,
}

impl Check {
    pub fn command(self) -> &'static str {
        match self {
            Self::Health => "health",
            Self::WorkerProbe => "worker_probe",
            Self::VaultProbe => "vault_probe",
        }
    }
    pub fn report_name(self) -> &'static str {
        match self {
            Self::Health => "health",
            Self::WorkerProbe => "worker",
            Self::VaultProbe => "vault",
        }
    }
}

// serde_json::Value normally accepts duplicate object keys. Reject them at every
// depth before checking the response schema, instead of silently taking the last.
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct UniqueVisitor;
        impl<'de> Visitor<'de> for UniqueVisitor {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("a JSON value without duplicate keys")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(Value::Bool(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(Value::Number(v.into())))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(Value::Number(v.into())))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Unique, E> {
                Number::from_f64(v)
                    .map(|v| Unique(Value::Number(v)))
                    .ok_or_else(|| E::custom("invalid number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(Value::String(v.to_owned())))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Unique, E> {
                Ok(Unique(Value::String(v)))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Unique, A::Error> {
                let mut result = Vec::new();
                while let Some(Unique(value)) = seq.next_element()? {
                    result.push(value);
                }
                Ok(Unique(Value::Array(result)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Unique, A::Error> {
                let mut result = Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if result.contains_key(&key) {
                        return Err(de::Error::custom("duplicate key"));
                    }
                    let Unique(value) = map.next_value()?;
                    result.insert(key, value);
                }
                Ok(Unique(Value::Object(result)))
            }
        }
        deserializer.deserialize_any(UniqueVisitor)
    }
}

pub fn parse(bytes: &[u8]) -> Result<Value, &'static str> {
    if bytes.is_empty() || bytes.len() > MAX_FRAME {
        return Err("SIDECAR_INVALID_RESPONSE");
    }
    serde_json::from_slice::<Unique>(bytes)
        .map(|v| v.0)
        .map_err(|_| "SIDECAR_INVALID_RESPONSE")
}

pub fn read_frame<R: BufRead>(reader: &mut R) -> io::Result<Option<Vec<u8>>> {
    let mut frame = Vec::with_capacity(512);
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if frame.is_empty() {
                Ok(None)
            } else {
                Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "unterminated frame",
                ))
            };
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let length = newline.map_or(available.len(), |position| position + 1);
        if frame.len() + length > MAX_FRAME + 1 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "oversized frame",
            ));
        }
        frame.extend_from_slice(&available[..length]);
        reader.consume(length);
        if newline.is_some() {
            frame.pop();
            return Ok(Some(frame));
        }
    }
}

fn exact(value: &Value, fields: &[&str]) -> bool {
    value.as_object().is_some_and(|map| {
        map.len() == fields.len() && fields.iter().all(|field| map.contains_key(*field))
    })
}
fn positive(value: &Value) -> bool {
    value
        .as_u64()
        .is_some_and(|n| n > 0 && n <= 9_007_199_254_740_991)
}
fn code(value: &Value) -> bool {
    value.as_str().is_some_and(|text| {
        !text.is_empty()
            && text.len() <= 64
            && text
                .bytes()
                .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
    })
}
fn python_version(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        let parts: Vec<_> = s.split('.').collect();
        parts.len() == 3
            && parts
                .iter()
                .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
    })
}

pub fn validate_ready(value: &Value, pid: u32) -> bool {
    exact(value, &["kind", "protocol_version", "pid"])
        && value["kind"] == "ready"
        && value["protocol_version"].as_u64() == Some(PROTOCOL)
        && value["pid"].as_u64() == Some(pid.into())
}

pub fn validate_response(value: &Value, request_id: &str, check: Check, sidecar_pid: u32) -> bool {
    if value["protocol_version"].as_u64() != Some(PROTOCOL)
        || value["request_id"] != request_id
        || value["workspace_id"] != WORKSPACE
        || value["command"] != check.command()
    {
        return false;
    }
    if value["ok"] == false {
        return exact(
            value,
            &[
                "protocol_version",
                "request_id",
                "workspace_id",
                "command",
                "ok",
                "error",
            ],
        ) && exact(&value["error"], &["code", "message"])
            && code(&value["error"]["code"])
            && value["error"]["message"]
                .as_str()
                .is_some_and(|s| s.len() < 256);
    }
    if value["ok"] != true
        || !exact(
            value,
            &[
                "protocol_version",
                "request_id",
                "workspace_id",
                "command",
                "ok",
                "result",
            ],
        )
    {
        return false;
    }
    let result = &value["result"];
    match check {
        Check::Health => {
            exact(
                result,
                &[
                    "status",
                    "code",
                    "pid",
                    "platform",
                    "python_version",
                    "frozen",
                    "product_integration",
                ],
            ) && result["status"] == "PASS"
                && result["code"] == "SIDECAR_READY"
                && result["pid"].as_u64() == Some(sidecar_pid.into())
                && matches!(
                    result["platform"].as_str(),
                    Some("win32" | "linux" | "darwin")
                )
                && python_version(&result["python_version"])
                && result["frozen"].is_boolean()
                && result["product_integration"] == "NOT_RUN"
        }
        Check::WorkerProbe => {
            exact(
                result,
                &[
                    "status",
                    "code",
                    "pid",
                    "parent_pid",
                    "artifact_bytes",
                    "artifact_sha256",
                    "temporary_artifact_removed",
                ],
            ) && result["status"] == "PASS"
                && result["code"] == "WORKER_PROBE_OK"
                && positive(&result["pid"])
                && result["pid"].as_u64() != Some(sidecar_pid.into())
                && result["parent_pid"].as_u64() == Some(sidecar_pid.into())
                && result["artifact_bytes"].as_u64() == Some(42)
                && result["artifact_sha256"] == SAMPLE_SHA256
                && result["temporary_artifact_removed"] == true
        }
        Check::VaultProbe => {
            exact(result, &["status", "backend", "code"])
                && result["backend"] == "windows-user-dpapi"
                && code(&result["code"])
                && matches!(result["status"].as_str(), Some("PASS" | "NOT_RUN" | "FAIL"))
                && (result["status"] != "PASS" || result["code"] == "VAULT_ROUNDTRIP_OK")
        }
    }
}

pub fn failure(check: Check, request_id: &str, code: &str) -> Value {
    json!({"protocol_version": PROTOCOL, "request_id": request_id, "workspace_id": WORKSPACE, "command": check.command(), "ok": false, "error": {"code": code, "message": "요청을 처리하지 못했습니다. 오류 코드를 확인하세요."}})
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn rejects_duplicate_keys_invalid_utf8_and_oversized_frames() {
        for bytes in [
            br#"{"kind":"ready","kind":"ready"}"#.as_slice(),
            br#"{"nested":{"ok":1,"ok":2}}"#.as_slice(),
            &[255_u8],
        ] {
            assert!(parse(bytes).is_err());
        }
        assert!(parse(&vec![b' '; MAX_FRAME + 1]).is_err());
        assert!(read_frame(&mut Cursor::new(vec![b'x'; MAX_FRAME + 2])).is_err());
        assert!(read_frame(&mut Cursor::new(b"{}")).is_err());
    }

    #[test]
    fn frame_reader_preserves_following_frame_and_accepts_windows_newline() {
        let mut input =
            Cursor::new(b"{\"kind\":\"ready\",\"protocol_version\":1,\"pid\":7}\r\n{}\n");
        let ready = parse(&read_frame(&mut input).unwrap().unwrap()).unwrap();
        assert!(validate_ready(&ready, 7));
        assert!(!validate_ready(&ready, 8));
        assert_eq!(read_frame(&mut input).unwrap().unwrap(), b"{}");
        assert!(read_frame(&mut input).unwrap().is_none());
    }

    #[test]
    fn rejects_cross_request_workspace_protocol_and_forged_worker_effects() {
        let valid = json!({"protocol_version":1,"request_id":"request","workspace_id":WORKSPACE,"command":"worker_probe","ok":true,"result":{"status":"PASS","code":"WORKER_PROBE_OK","pid":8,"parent_pid":7,"artifact_bytes":42,"artifact_sha256":SAMPLE_SHA256,"temporary_artifact_removed":true}});
        assert!(validate_response(&valid, "request", Check::WorkerProbe, 7));
        for (field, replacement) in [
            ("protocol_version", json!(2)),
            ("request_id", json!("other")),
            ("workspace_id", json!("other")),
            ("command", json!("health")),
        ] {
            let mut broken = valid.clone();
            broken[field] = replacement;
            assert!(!validate_response(
                &broken,
                "request",
                Check::WorkerProbe,
                7
            ));
        }
        for (field, replacement) in [
            ("pid", json!(7)),
            ("parent_pid", json!(9)),
            ("artifact_bytes", json!(41)),
            ("artifact_sha256", json!("0".repeat(64))),
            ("temporary_artifact_removed", json!(false)),
        ] {
            let mut broken = valid.clone();
            broken["result"][field] = replacement;
            assert!(!validate_response(
                &broken,
                "request",
                Check::WorkerProbe,
                7
            ));
        }
        let mut extra = valid;
        extra["capability"] = json!("must not leak");
        assert!(!validate_response(&extra, "request", Check::WorkerProbe, 7));
    }

    #[test]
    fn renderer_can_select_only_three_fixed_checks() {
        for check in ["health", "worker_probe", "vault_probe"] {
            assert!(serde_json::from_value::<Check>(json!(check)).is_ok());
        }
        for value in [
            json!("shell"),
            json!({"check":"health","path":"x"}),
            json!(null),
            json!(42),
        ] {
            assert!(serde_json::from_value::<Check>(value).is_err());
        }
    }
}

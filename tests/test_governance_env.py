"""Governance input preparation tests with synthetic data only.

No provider, credential, network, VM/AGS installation or MCP server is used.
The optional RealVmContractTests class runs only when VM sources and the
cryptography package are importable; otherwise it is reported as NOT_RUN.
Linux results are development verification, not Windows support evidence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_governance_env import (  # noqa: E402
    BLOCKED, READY, GovernanceEnvError, a2_digest, build_model_class_table, canonical_json,
    check_a2_config, check_model_class_table, check_node, compose_a2_config, inspect_plugin_root,
    prepare_governance_environment, write_atomic,
)
from aik_governance_env import contract  # noqa: E402


SYNTHETIC_PRIVATE_VALUE = base64.b64encode(b"SYNTHETIC-PRIVATE-FIELD-NOT-A-REAL-KEY").decode("ascii")
SYNTHETIC_PUBLIC_VALUE = base64.b64encode(b"SYNTHETIC-PUBLIC-FIELD").decode("ascii")


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def flip_last(value: str) -> str:
    return value[:-1] + ("0" if value[-1] != "0" else "1")


class TempDirMixin:
    def make_temp(self) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="aik-governance-env-")
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)


class A2Fixture(TempDirMixin):
    def make_a2_files(self, directory: Path, *, private_value=SYNTHETIC_PRIVATE_VALUE,
                      public_value=SYNTHETIC_PUBLIC_VALUE):
        key = directory / "producer-key.json"
        pin = directory / "producer-pin.json"
        state = directory / "producer-state"
        write_json(key, {"version": 1, "namespace": contract.A2_PROFILE_ID, "keyId": "synthetic-key-1",
                         "hostId": "flowmarshal", "privateKeyPkcs8": private_value})
        write_json(pin, {"namespace": contract.A2_PROFILE_ID, "pins": [
            {"keyId": "synthetic-key-1", "publicKeySpki": public_value, "status": "active"}]})
        return key, pin, state

    def compose(self, key: Path, pin: Path, state: Path) -> bytes:
        return compose_a2_config(key_location=str(key), pin_location=str(pin), state_location=str(state))


class CanonicalTests(unittest.TestCase):
    def test_keys_sort_by_utf16_code_units_including_hangul_and_emoji(self) -> None:
        value = {"\uff71": 5, "\U0001F600": 4, "가": 3, "b": 1, "a": [True, False, None, -7, '따옴"표']}
        expected = '{"a":[true,false,null,-7,"따옴\\"표"],"b":1,"가":3,"\U0001F600":4,"\uff71":5}'
        self.assertEqual(canonical_json(value), expected)
        # Code-point order differs (U+FF71 before U+1F600), so this is not plain sort_keys.
        self.assertNotEqual(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), expected)
        self.assertEqual(a2_digest(value), "sha256:" + hashlib.sha256(expected.encode("utf-8")).hexdigest())

    def test_unsupported_values_are_rejected(self) -> None:
        self.assertEqual(canonical_json(2**53 - 1), str(2**53 - 1))
        for value in (1.5, 2**53, -(2**53), {1: "x"}, "\ud800", (1, 2), object(), {"k": 0.0}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(GovernanceEnvError) as caught:
                    canonical_json(value)
                self.assertEqual(caught.exception.code, "CANONICAL_INPUT_UNSUPPORTED")


class ModelClassTests(unittest.TestCase):
    OBSERVED = ("model-a", "model-b", "model-c")

    def test_build_and_check_round_trip(self) -> None:
        raw = build_model_class_table({"model-b": "deep", "model-a": "general"}, observed_models=self.OBSERVED)
        self.assertTrue(raw.endswith(b"\n") and not raw.endswith(b"\n\n"))
        self.assertEqual(json.loads(raw), {"format": contract.MODEL_CLASSES_FORMAT,
                                           "classes": {"model-a": "general", "model-b": "deep"}})
        classes, digest = check_model_class_table(raw)
        self.assertEqual(classes, {"model-a": "general", "model-b": "deep"})
        self.assertEqual(digest, "sha256:" + hashlib.sha256(raw).hexdigest())

    def test_build_rejections(self) -> None:
        cases = (
            ({"model-x": "general"}, "MODEL_NOT_OBSERVED"),
            ({"model-a": "huge"}, "INVALID_MODEL_CLASS"),
            ({"": "general"}, "INVALID_MODEL_NAME"),
            ({"   ": "general"}, "INVALID_MODEL_NAME"),
            ({}, "EMPTY_MODEL_CLASSES"),
        )
        for assignments, code in cases:
            with self.subTest(code=code, assignments=assignments):
                with self.assertRaises(GovernanceEnvError) as caught:
                    build_model_class_table(assignments, observed_models=self.OBSERVED)
                self.assertEqual(caught.exception.code, code)

    def test_check_rejections_including_duplicate_keys(self) -> None:
        fmt = contract.MODEL_CLASSES_FORMAT
        raws = (
            f'{{"format":"{fmt}","classes":{{"m":"general","m":"deep"}}}}'.encode(),
            f'{{"format":"{fmt}","format":"{fmt}","classes":{{"m":"general"}}}}'.encode(),
            json.dumps({"format": fmt, "classes": {"m": "general"}, "extra": 1}).encode(),
            json.dumps({"format": "other-format", "classes": {"m": "general"}}).encode(),
            json.dumps({"format": fmt, "classes": {}}).encode(),
            json.dumps({"format": fmt, "classes": {"m": "huge"}}).encode(),
            json.dumps({"format": fmt, "classes": {"  ": "general"}}).encode(),
            json.dumps({"format": fmt, "classes": ["m"]}).encode(),
            b"{",
        )
        for raw in raws:
            with self.subTest(raw=raw[:60]):
                with self.assertRaises(GovernanceEnvError) as caught:
                    check_model_class_table(raw)
                self.assertEqual(caught.exception.code, "GOVERNANCE_CONTRACT_MISMATCH")
                self.assertEqual(caught.exception.check, "model_class_table")


class A2ProfileTests(A2Fixture, unittest.TestCase):
    def setUp(self) -> None:
        self.directory = self.make_temp()
        self.key, self.pin, self.state = self.make_a2_files(self.directory)
        self.raw = self.compose(self.key, self.pin, self.state)

    def assert_code(self, raw: bytes, code: str) -> GovernanceEnvError:
        with self.assertRaises(GovernanceEnvError) as caught:
            check_a2_config(raw)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(SYNTHETIC_PRIVATE_VALUE, str(caught.exception))
        self.assertNotIn(SYNTHETIC_PRIVATE_VALUE, repr(caught.exception))
        return caught.exception

    def tamper(self, mutate, *, refreeze: bool = False) -> bytes:
        config = json.loads(self.raw)
        mutate(config)
        if refreeze:
            selection = config["selection"]
            selection["freezeIdentity"] = a2_digest(
                {key: value for key, value in selection.items() if key != "freezeIdentity"})
        return json.dumps(config, ensure_ascii=False).encode("utf-8")

    def test_compose_then_check_round_trip(self) -> None:
        config = json.loads(self.raw)
        self.assertEqual(set(config), {"format", "selection", "resources"})
        self.assertEqual(config["selection"]["profile"], dict(contract.A2_PROFILE))
        report = check_a2_config(self.raw)
        self.assertEqual(report.profile_id, contract.A2_PROFILE_ID)
        self.assertEqual(report.key_id, "synthetic-key-1")
        self.assertEqual(report.key_pin_match, "NOT_CHECKED")
        self.assertEqual(report.assurance, "same-user")
        self.assertEqual(report.freeze_identity, config["selection"]["freezeIdentity"])
        self.assertEqual(report.pin_set_digest, a2_digest(json.loads(self.pin.read_text(encoding="utf-8"))))
        self.assertNotIn(SYNTHETIC_PRIVATE_VALUE, repr(report))
        self.assertNotIn(SYNTHETIC_PRIVATE_VALUE.encode(), self.raw)

    def test_compose_never_opens_the_key_file(self) -> None:
        missing_key = self.directory / "not-yet-provisioned-key.json"
        raw = self.compose(missing_key, self.pin, self.state)
        self.assert_code(raw, "A2_PRODUCER_KEY_UNAVAILABLE")

    def test_config_field_tampering_maps_to_vm_codes(self) -> None:
        other_state = str(self.directory / "other-state")
        cases = (
            (lambda c: c["selection"].__setitem__("source", "other"), False, "A2_PRODUCER_PROFILE_MISMATCH"),
            (lambda c: c["selection"]["profile"].__setitem__("hostId", "x"), False, "A2_PRODUCER_PROFILE_MISMATCH"),
            (lambda c: c["selection"]["profile"].__setitem__("extra", "x"), False, "A2_PRODUCER_PROFILE_MISMATCH"),
            (lambda c: c["selection"].__setitem__("extra", "x"), False, "A2_PRODUCER_PROFILE_MISMATCH"),
            (lambda c: c["resources"]["key"].__setitem__("namespace", "x"), False, "A2_PRODUCER_NAMESPACE_MISMATCH"),
            (lambda c: c["resources"].__setitem__("extra", {}), False, "A2_PRODUCER_NAMESPACE_MISMATCH"),
            (lambda c: c["resources"]["state"].__setitem__("location", other_state), False,
             "A2_PRODUCER_RESOURCE_BINDING_MISMATCH"),
            (lambda c: c["selection"].__setitem__("resourceBindingDigest", "sha256:" + "0" * 64), False,
             "A2_PRODUCER_RESOURCE_BINDING_MISMATCH"),
            (lambda c: c["selection"].__setitem__("freezeIdentity", flip_last(c["selection"]["freezeIdentity"])),
             False, "A2_PRODUCER_FREEZE_IDENTITY_MISMATCH"),
            (lambda c: c["selection"].__setitem__("pinSetDigest", "sha256:" + "1" * 64), True,
             "A2_PRODUCER_PIN_SET_DIGEST_MISMATCH"),
            (lambda c: c.__setitem__("extra", 1), False, "A2_PRODUCER_PROFILE_MISMATCH"),
            (lambda c: c.__setitem__("format", "other"), False, "A2_PRODUCER_PROFILE_MISMATCH"),
            (lambda c: c["resources"]["key"].__setitem__("location", "/etc/flowmarshal/ags-producer-key.json"),
             False, "PROTECTED_PRODUCER_PATH"),
        )
        for mutate, refreeze, code in cases:
            with self.subTest(code=code):
                self.assert_code(self.tamper(mutate, refreeze=refreeze), code)

    def test_invalid_and_duplicate_json_config(self) -> None:
        self.assert_code(b"{", "A2_PRODUCER_CONFIG_JSON_INVALID")
        self.assert_code(b'{"format":"a","format":"b"}', "A2_PRODUCER_CONFIG_JSON_INVALID")
        self.assert_code(b"[]", "A2_PRODUCER_CONFIG_JSON_INVALID")

    def test_key_file_shape_rejections_never_reveal_the_private_field(self) -> None:
        base = {"version": 1, "namespace": contract.A2_PROFILE_ID, "keyId": "synthetic-key-1",
                "hostId": "flowmarshal", "privateKeyPkcs8": SYNTHETIC_PRIVATE_VALUE}
        cases = (
            {**base, "version": 2},
            {**base, "version": True},
            {**base, "hostId": "other"},
            {**base, "namespace": "other"},
            {**base, "keyId": " "},
            {**base, "extra": "x"},
            {**base, "privateKeyPkcs8": "!!!" + SYNTHETIC_PRIVATE_VALUE},
        )
        for document in cases:
            with self.subTest(document=sorted(document)):
                write_json(self.key, document)
                self.assert_code(self.raw, "A2_PRODUCER_KEY_MISMATCH")
        self.key.unlink()
        self.assert_code(self.raw, "A2_PRODUCER_KEY_UNAVAILABLE")

    def test_pin_file_rejections(self) -> None:
        entry = {"keyId": "synthetic-key-1", "publicKeySpki": SYNTHETIC_PUBLIC_VALUE, "status": "active"}
        ns = contract.A2_PROFILE_ID
        write_json(self.pin, {"namespace": ns, "pins": [{**entry, "status": "revoked"}]})
        self.assert_code(self.raw, "A2_PRODUCER_PIN_MISMATCH")
        write_json(self.pin, {"namespace": ns, "pins": [entry, dict(entry)]})
        self.assert_code(self.raw, "A2_PRODUCER_PIN_MISMATCH")
        write_json(self.pin, {"namespace": ns, "pins": [{**entry, "publicKeySpki": "%%%"}]})
        self.assert_code(self.raw, "A2_PRODUCER_PIN_MISMATCH")
        # Structurally valid but different pin set: bound digest no longer matches.
        write_json(self.pin, {"namespace": ns, "pins": [entry, {**entry, "keyId": "synthetic-key-2"}]})
        self.assert_code(self.raw, "A2_PRODUCER_PIN_SET_DIGEST_MISMATCH")
        self.pin.unlink()
        self.assert_code(self.raw, "A2_PRODUCER_PIN_UNAVAILABLE")

    def test_protected_and_invalid_locations_are_rejected_in_compose(self) -> None:
        for kwargs in (
            {"key_location": "/etc/flowmarshal/ags-producer-key.json"},
            {"pin_location": "/etc/agent-governance-suite/vm-operator-policy.json"},
            {"pin_location": "C:\\ProgramData\\agent-governance-suite\\vm-operator-policy.json"},
            {"key_location": "c:\\programdata\\flowmarshal\\ags-producer-key.json"},
        ):
            with self.subTest(kwargs=kwargs):
                arguments = {"key_location": str(self.key), "pin_location": str(self.pin),
                             "state_location": str(self.state), **kwargs}
                with self.assertRaises(GovernanceEnvError) as caught:
                    compose_a2_config(**arguments)
                self.assertEqual(caught.exception.code, "PROTECTED_PRODUCER_PATH")
        unnormalized = str(self.directory) + os.sep + ".." + os.sep + self.directory.name + os.sep + "pin.json"
        for kwargs in ({"pin_location": "relative/pin.json"}, {"pin_location": unnormalized},
                       {"state_location": ""}):
            with self.subTest(kwargs=kwargs):
                arguments = {"key_location": str(self.key), "pin_location": str(self.pin),
                             "state_location": str(self.state), **kwargs}
                with self.assertRaises(GovernanceEnvError) as caught:
                    compose_a2_config(**arguments)
                kind = next(iter(kwargs)).split("_")[0]
                self.assertEqual(caught.exception.code, f"A2_PRODUCER_{kind.upper()}_PATH_INVALID")

    def test_symlinked_pin_file_is_rejected(self) -> None:
        link = self.directory / "pin-link.json"
        try:
            link.symlink_to(self.pin)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links unavailable: {error}")
        with self.assertRaises(GovernanceEnvError) as caught:
            self.compose(self.key, link, self.state)
        self.assertEqual(caught.exception.code, "A2_PRODUCER_PIN_UNAVAILABLE")


class PluginRootFixture(TempDirMixin):
    def make_plugin(self, root: Path, mutate=None) -> Path:
        (root / "bin").mkdir(parents=True)
        (root / "lib").mkdir()
        for entry_id in contract.REQUIRED_ENTRY_POINTS:
            (root / "bin" / f"{entry_id}.mjs").write_text(f"// synthetic {entry_id}\n", encoding="utf-8")
        (root / "lib" / "shared.mjs").write_text("// synthetic shared\n", encoding="utf-8")
        manifest = {"format": contract.MANIFEST_FORMAT, "entryPoints": [
            {"id": entry_id, "path": f"bin/{entry_id}.mjs",
             "executionClosure": [f"bin/{entry_id}.mjs", "lib/shared.mjs"]}
            for entry_id in contract.REQUIRED_ENTRY_POINTS]}
        if mutate is not None:
            mutate(manifest)
        write_json(root / contract.MANIFEST, manifest)
        return root


class PluginRootTests(PluginRootFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.base = self.make_temp()

    def assert_check(self, root: Path, check: str) -> None:
        with self.assertRaises(GovernanceEnvError) as caught:
            inspect_plugin_root(root)
        self.assertEqual(caught.exception.code, "GOVERNANCE_CONTRACT_MISMATCH")
        self.assertEqual(caught.exception.check, check)

    def test_valid_fake_manifest_is_inspected_with_local_digests(self) -> None:
        root = self.make_plugin(self.base / "plugin")
        report = inspect_plugin_root(root)
        self.assertEqual(report.provenance, "local_derived")
        self.assertEqual(report.file_count, 5)
        resolved = root.resolve()
        files = sorted(
            (path.relative_to(resolved).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in [*(resolved / "bin").iterdir(), resolved / "lib" / "shared.mjs"])
        tree = "".join(f"{p}\0{h}\n" for p, h in files)
        self.assertEqual(report.closure_tree_digest, "sha256:" + hashlib.sha256(tree.encode()).hexdigest())
        self.assertEqual(report.manifest_sha256, "sha256:" + hashlib.sha256(
            (root / contract.MANIFEST).read_bytes()).hexdigest())

    def test_format_mismatch(self) -> None:
        root = self.make_plugin(self.base / "plugin", lambda m: m.__setitem__("format", "other"))
        self.assert_check(root, "manifest_format")

    def test_missing_entry_point(self) -> None:
        root = self.make_plugin(self.base / "plugin", lambda m: m.__setitem__(
            "entryPoints", [e for e in m["entryPoints"] if e["id"] != "acceptance-cli"]))
        self.assert_check(root, "entry_point:acceptance-cli")

    def test_closure_outside_root(self) -> None:
        (self.base / "outside.mjs").write_text("// outside\n", encoding="utf-8")
        root = self.make_plugin(self.base / "plugin", lambda m: m["entryPoints"][0]["executionClosure"]
                                .append("../outside.mjs"))
        self.assert_check(root, "closure_file")

    def test_missing_host_integration_manifest_blocks(self) -> None:
        root = self.make_plugin(self.base / "plugin")
        (root / contract.MANIFEST).unlink()
        self.assert_check(root, "manifest")
        self.assert_check(self.base / "absent-root", "manifest")

    def test_check_node_with_injected_runner(self) -> None:
        calls = []

        def runner_for(stdout="", returncode=0, raises=None):
            def runner(args, **kwargs):
                calls.append((args, kwargs))
                if raises is not None:
                    raise raises
                return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")
            return runner

        report = check_node("/synthetic/node", runner=runner_for("v24.1.0\n"))
        self.assertEqual((report.state, report.version), ("READY", (24, 1, 0)))
        self.assertEqual(calls[-1][0], ["/synthetic/node", "--version"])
        self.assertEqual(calls[-1][1]["timeout"], 10.0)
        self.assertEqual(check_node("n", runner=runner_for("v22.9.0")).code, "NODE_VERSION_UNSUPPORTED")
        self.assertEqual(check_node("n", runner=runner_for("garbage")).code, "NODE_VERSION_UNREADABLE")
        self.assertEqual(check_node("n", runner=runner_for(returncode=1)).code, "NODE_UNAVAILABLE")
        self.assertEqual(check_node("n", runner=runner_for(raises=FileNotFoundError())).code, "NODE_UNAVAILABLE")
        self.assertEqual(check_node("n", runner=runner_for(raises=subprocess.TimeoutExpired("n", 10))).code,
                         "NODE_TIMEOUT")


class EnvironmentTests(A2Fixture, PluginRootFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.base = self.make_temp()
        self.plugin = self.make_plugin(self.base / "plugin")
        self.table = write_atomic(self.base / "model-classes.json", build_model_class_table(
            {"model-a": "general"}, observed_models=["model-a"]))
        key, pin, state = self.make_a2_files(self.base)
        self.a2 = write_atomic(self.base / "a2-profile.json", self.compose(key, pin, state))

    def prepare(self, **overrides):
        arguments = {"plugin_root": self.plugin, "model_class_table_path": self.table,
                     "a2_config_path": self.a2, **overrides}
        return prepare_governance_environment(**arguments)

    def test_all_ready_yields_exactly_three_variables(self) -> None:
        environment = self.prepare()
        self.assertTrue(environment.ready)
        self.assertEqual(environment.env(), {
            contract.ENV_PLUGIN_ROOT: str(self.plugin),
            contract.ENV_MODEL_CLASSES: str(self.table),
            contract.ENV_A2_PROFILE: str(self.a2),
        })
        self.assertEqual(environment.model_class_names, ("model-a",))
        self.assertEqual(environment.a2.key_pin_match, "NOT_CHECKED")

    def test_any_blocked_input_refuses_env_without_partial_values(self) -> None:
        (self.plugin / contract.MANIFEST).unlink()
        environment = self.prepare()
        self.assertFalse(environment.ready)
        states = {s.variable: (s.state, s.code, s.check) for s in environment.statuses}
        self.assertEqual(states[contract.ENV_PLUGIN_ROOT], (BLOCKED, "GOVERNANCE_CONTRACT_MISMATCH", "manifest"))
        self.assertEqual(states[contract.ENV_MODEL_CLASSES][0], READY)
        self.assertEqual(states[contract.ENV_A2_PROFILE][0], READY)
        with self.assertRaises(GovernanceEnvError) as caught:
            environment.env()
        self.assertEqual(caught.exception.code, "GOVERNANCE_ENVIRONMENT_BLOCKED")

    def test_relative_or_damaged_inputs_block(self) -> None:
        environment = self.prepare(model_class_table_path="relative/model-classes.json")
        status = [s for s in environment.statuses if s.variable == contract.ENV_MODEL_CLASSES][0]
        self.assertEqual((status.state, status.code), (BLOCKED, "PATH_NOT_ABSOLUTE"))
        broken = write_atomic(self.base / "broken-a2.json", b"{}")
        environment = self.prepare(a2_config_path=broken)
        status = [s for s in environment.statuses if s.variable == contract.ENV_A2_PROFILE][0]
        self.assertEqual((status.state, status.code), (BLOCKED, "A2_PRODUCER_PROFILE_MISMATCH"))
        with self.assertRaises(GovernanceEnvError):
            environment.env()

    def test_write_atomic_refuses_overwrite_by_default(self) -> None:
        target = self.base / "output.json"
        write_atomic(target, b"first\n")
        with self.assertRaises(GovernanceEnvError) as caught:
            write_atomic(target, b"second\n")
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(target.read_bytes(), b"first\n")
        write_atomic(target, b"second\n", exclusive=False)
        self.assertEqual(target.read_bytes(), b"second\n")
        self.assertEqual([p.name for p in self.base.iterdir() if p.name.endswith(".tmp")], [])
        with self.assertRaises(GovernanceEnvError) as caught:
            write_atomic("relative.json", b"x")
        self.assertEqual(caught.exception.code, "OUTPUT_PATH_INVALID")


class RealVmContractTests(A2Fixture, unittest.TestCase):
    """Runs only when VM sources (AIK_VM_SRC or ../vibemarshal/src) and cryptography import."""

    @classmethod
    def setUpClass(cls) -> None:
        vm_src = Path(os.environ.get("AIK_VM_SRC") or (ROOT.parent / "vibemarshal" / "src"))
        if vm_src.is_dir() and str(vm_src) not in sys.path:
            sys.path.insert(0, str(vm_src))
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            raise unittest.SkipTest("NOT_RUN: cryptography is not installed.") from None
        try:
            from flowmarshal.engine import ags_observation_producer, governance_gate
        except Exception:
            raise unittest.SkipTest("NOT_RUN: VM flowmarshal sources are not importable.") from None
        cls.serialization = serialization
        cls.key_class = Ed25519PrivateKey
        cls.producer_module = ags_observation_producer
        cls.gate_module = governance_gate

    def test_closure_tree_digest_matches_the_vm_preflight_formula(self) -> None:
        """Same keys (manifest strings, including './'), same per-file hex and VM sha256_bytes."""
        with tempfile.TemporaryDirectory(prefix="aik-gov-tree-") as name:
            root = Path(name) / "plugin"
            declared = {}
            for entry_id in ("mcp-server", "scope-baseline", "scope-compare", "acceptance-cli"):
                path = f"dist/{entry_id}.mjs"
                (root / "dist").mkdir(parents=True, exist_ok=True)
                (root / path).write_bytes(f"// {entry_id}\n".encode("utf-8"))
                declared[entry_id] = path
            (root / "lib").mkdir()
            (root / "lib" / "shared.mjs").write_bytes(b"export const x = 1;\n")
            manifest = {"format": "agent-governance-suite.host-integration.v1", "entryPoints": [
                {"id": entry_id, "path": path, "executionClosure": ["./lib/shared.mjs"]}
                for entry_id, path in declared.items()]}
            (root / "host-integration.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = inspect_plugin_root(root)
            files = set(declared.values()) | {"./lib/shared.mjs"}
            gate = self.gate_module
            expected = gate.sha256_bytes("".join(
                f"{relative}\0{gate._sha256_file((root / relative).resolve())}\n"
                for relative in sorted(files)).encode("utf-8"))
            self.assertEqual(report.closure_tree_digest, expected)
            self.assertEqual(report.file_count, len(files))

    def make_real_key_files(self, directory: Path):
        serialization = self.serialization
        private = self.key_class.generate()
        pkcs8 = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
        spki = private.public_key().public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)
        del private
        files = self.make_a2_files(directory, private_value=base64.b64encode(pkcs8).decode("ascii"),
                                   public_value=base64.b64encode(spki).decode("ascii"))
        del pkcs8
        os.chmod(files[0], 0o600)
        return files

    def test_vm_accepts_aik_a2_config_and_rejects_the_same_tampering(self) -> None:
        directory = self.make_temp()
        key, pin, state = self.make_real_key_files(directory)
        raw = self.compose(key, pin, state)
        config_path = write_atomic(directory / "a2-profile.json", raw)
        producer = self.producer_module._load_a2_producer(config_path)
        self.assertIsNotNone(producer)
        check_a2_config(raw)

        config = json.loads(raw)
        config["selection"]["freezeIdentity"] = flip_last(config["selection"]["freezeIdentity"])
        tampered = json.dumps(config).encode("utf-8")
        tampered_path = write_atomic(directory / "a2-profile-tampered.json", tampered)
        expected = "A2_PRODUCER_FREEZE_IDENTITY_MISMATCH"
        vm_error = getattr(self.producer_module, "ProducerUnavailable", Exception)
        with self.assertRaises(vm_error) as vm_caught:
            self.producer_module._load_a2_producer(tampered_path)
        vm_text = f"{getattr(vm_caught.exception, 'code', '')} {vm_caught.exception.args}"
        self.assertIn(expected, vm_text)
        with self.assertRaises(GovernanceEnvError) as aik_caught:
            check_a2_config(tampered)
        self.assertEqual(aik_caught.exception.code, expected)

    def test_vm_accepts_aik_model_class_table_with_matching_digest(self) -> None:
        directory = self.make_temp()
        raw = build_model_class_table({"synthetic-model-a": "general", "synthetic-model-b": "deep"},
                                      observed_models=["synthetic-model-a", "synthetic-model-b"])
        path = write_atomic(directory / "model-classes.json", raw)
        vm_classes, vm_digest = self.gate_module.load_model_classes(path)
        aik_classes, aik_digest = check_model_class_table(raw)
        self.assertEqual(dict(vm_classes), aik_classes)
        vm_digest = str(vm_digest)
        self.assertEqual(aik_digest, vm_digest)  # VM sha256_bytes carries the sha256: prefix


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Protocol
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

import omh.workflows.browser_skill_promotion_plan as promotion_plan
import omh.workflows.browser_skill_promotion as lifecycle
from omh.workflows.browser_skill_promotion_plan import (
    BrowserSkillPromotionPlanError,
    build_browser_skill_promotion_plan,
    read_browser_skill_package,
)
from omh.workflows.browser_workflow_learning import JsonObject, JsonValue
from omh.workflows.browser_workflow_learning_store import (
    approve_browser_workflow_trace,
    read_browser_workflow_trace,
    replay_stored_browser_workflow_trace,
    resolved_browser_workflow_promotion_reference,
    write_browser_workflow_trace,
)


class _PromotionValuePort(Protocol):
    """Exact producer accessors, structurally checked without exporting internals."""

    def _digest_value(self, value: object, label: str) -> str: ...
    def _mapping(self, value: Mapping[str, object], key: str) -> dict[str, object]: ...
    def _text(self, value: Mapping[str, object], key: str) -> dict[str, str]: ...


class _PromotionValueProbe(_PromotionValuePort, ABC):
    @staticmethod
    def bind(module: _PromotionValuePort) -> tuple[
        Callable[[object, str], str],
        Callable[[Mapping[str, object], str], dict[str, object]],
        Callable[[Mapping[str, object], str], dict[str, str]],
    ]:
        return module._digest_value, module._mapping, module._text


digest_value, mapping, text = _PromotionValueProbe.bind(lifecycle)
_digest_value, _mapping, _text = digest_value, mapping, text


# The default JSON decoder returns JSON values, not a caller-selected class.
_decode_json: Callable[[str | bytes], JsonValue] = json.loads


def json_object(raw: str | bytes) -> JsonObject:
    value = _decode_json(raw)
    assert isinstance(value, dict)
    return value


class DescriptorModule(Protocol):
    """A producer whose module-local OS dependency can be patched independently."""

    @property
    def os(self) -> ModuleType: ...


class BrowserSkillPromotionPlanTests(unittest.TestCase):
    def test_package_reads_preserve_raw_bytes_under_crt_text_translation(self) -> None:
        samples = (b"first\r\nsecond\r\n", b"before\x1aafter", b"x" * 65536 + b"\r\n\x1atail")
        with TemporaryDirectory() as temporary:
            target = Path(temporary)
            path = target / "SKILL.md"
            for raw in samples:
                for simulated in (False, True):
                    with self.subTest(raw_size=len(raw), simulated_crt=simulated):
                        _ = path.write_bytes(raw)
                        if simulated:
                            with _crt_descriptor_io(promotion_plan):
                                package = read_browser_skill_package(target)
                        else:
                            package = read_browser_skill_package(target)
                        self.assertEqual(package["SKILL.md"].encode("utf-8"), raw)
                        self.assertEqual(path.read_bytes(), raw)

    def test_plan_uses_real_git_bound_public_reference_and_is_deterministic(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            trace_id = trace["trace_id"]
            assert isinstance(trace_id, str)
            reference = resolved_browser_workflow_promotion_reference(root, trace_id)

            first = build_browser_skill_promotion_plan(root, trace_id, "checkout-confirmation")
            second = build_browser_skill_promotion_plan(root, trace_id, "checkout-confirmation")

            self.assertEqual(first, second)
            self.assertEqual(first["project_identity"], reference["project_identity"])
            self.assertEqual(_mapping(first, "source")["trace_digest"], reference["trace_digest"])
            self.assertEqual(_mapping(first, "source")["trace_revision"], reference["trace_revision"])
            self.assertEqual(_mapping(first, "source")["replay_digest"], reference["replay_digest"])
            self.assertEqual(first["target_path"], str(root / ".hermes" / "skills" / "checkout-confirmation"))
            self.assertLess(len(_text(first, "package")["SKILL.md"].encode("utf-8")), 8 * 1024)
            self.assertNotIn('"steps"', _text(first, "package")["SKILL.md"])
            self.assertIn("resources/", _text(first, "package")["SKILL.md"])
            self.assertEqual(
                _text(first, "package")["SKILL.md"],
                _text(first, "package")[f"resources/{first['generation']}/entry.md"],
            )
            self.assertNotIn(first["package_digest"], "\n".join(_text(first, "package").values()))

    def test_plan_reuses_generic_skill_draft_checks_without_activating_or_writing_a_draft(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            trace_id = trace["trace_id"]
            assert isinstance(trace_id, str)

            plan = build_browser_skill_promotion_plan(root, trace_id, "checkout-confirmation")

            self.assertTrue(_mapping(plan, "generic_draft_check")["ok"])
            self.assertEqual(_mapping(_mapping(plan, "generic_draft"), "lifecycle")["state"], "inactive")
            self.assertFalse(_mapping(_mapping(plan, "generic_draft"), "lifecycle")["installed"])
            self.assertEqual(plan["generic_draft_digest"], hashlib.sha256(
                _canonical(plan["generic_draft"])
            ).hexdigest())
            self.assertFalse((root / ".omh" / "learning" / "skill-drafts").exists())

    def test_diff_replaces_entry_and_adds_resources_without_deleting_unmanaged_files(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            trace_id = trace["trace_id"]
            assert isinstance(trace_id, str)
            target = root / ".hermes" / "skills" / "checkout-confirmation"
            target.mkdir(parents=True)
            _ = (target / "SKILL.md").write_text("old entry\n", encoding="utf-8")
            _ = (target / "obsolete.txt").write_text("remove me\n", encoding="utf-8")

            plan = build_browser_skill_promotion_plan(root, trace_id, "checkout-confirmation")

            diff = plan["diff"]
            assert isinstance(diff, str)
            self.assertIn(f"--- {target / 'SKILL.md'}", diff)
            self.assertIn(f"+++ {target / 'SKILL.md'}", diff)
            self.assertIn("-old entry", diff)
            self.assertNotIn(str(target / "obsolete.txt"), diff)
            self.assertNotIn("-remove me", diff)
            self.assertIn(f"+++ {target / 'resources' / _digest_value(plan['generation'], 'generation') / 'trace.json'}", diff)
            self.assertEqual(plan["diff_digest"], hashlib.sha256(diff.encode("utf-8")).hexdigest())

    def test_prior_generation_is_in_reviewed_entry_before_package_and_diff_digests(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            trace_id = trace["trace_id"]
            assert isinstance(trace_id, str)
            first = build_browser_skill_promotion_plan(root, trace_id, "checkout-confirmation")
            second = build_browser_skill_promotion_plan(
                root,
                trace_id,
                "checkout-confirmation",
                previous_generation=_digest_value(first["generation"], "generation"),
                existing_files=_text(first, "package"),
            )

            self.assertNotEqual(first["generation"], second["generation"])
            metadata = json_object(next(line.split(": ", 1)[1] for line in _text(second, "package")["SKILL.md"].splitlines() if line.startswith("omh_browser_promotion: ")))
            self.assertEqual(metadata["previous_generation"], first["generation"])
            self.assertNotEqual(first["package_digest"], second["package_digest"])
            self.assertNotEqual(first["diff_digest"], second["diff_digest"])
            manifest = _text(second, "package")[f"resources/{second['generation']}/manifest.json"]
            self.assertIn(hashlib.sha256(_text(second, "package")["SKILL.md"].encode()).hexdigest(), manifest)
            self.assertNotIn(hashlib.sha256(manifest.encode()).hexdigest(), manifest)

    def test_plan_refuses_any_non_passing_public_projection(self) -> None:
        with _project() as root:
            trace = _passing_trace(root)
            trace_id = trace["trace_id"]
            assert isinstance(trace_id, str)
            reference = dict(resolved_browser_workflow_promotion_reference(root, trace_id))
            for key, value in (("replay_status", "stale"), ("lifecycle_status", "quarantined"), ("trace_revision", 0)):
                with self.subTest(key=key):
                    changed = dict(reference)
                    changed[key] = value
                    with self.assertRaisesRegex(BrowserSkillPromotionPlanError, "promotion reference"):
                        _ = build_browser_skill_promotion_plan(
                            root,
                            trace_id,
                            "checkout-confirmation",
                            reference=changed,
                        )


@contextmanager
def _crt_descriptor_io(module: DescriptorModule) -> Generator[None, None, None]:
    """Simulate only CRT byte translation; files, fstat and bounds remain real.

    The fixture's CRLF pairs are within read chunks. This is not a complete
    Windows CRT emulator or evidence of native Windows execution.
    """
    native_binary = getattr(os, "O_BINARY", 0)
    binary = native_binary or (1 << 29)
    text_descriptors: dict[int, bool] = {}

    def open_descriptor(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = os.open(path, (flags & ~binary) | native_binary, mode, dir_fd=dir_fd)
        if not flags & binary:
            text_descriptors[descriptor] = False
        else:
            _ = text_descriptors.pop(descriptor, None)
        return descriptor

    def read_descriptor(descriptor: int, size: int) -> bytes:
        if text_descriptors.get(descriptor, False):
            return b""
        raw = os.read(descriptor, size)
        if descriptor in text_descriptors:
            if b"\x1a" in raw:
                raw = raw.split(b"\x1a", 1)[0]
                text_descriptors[descriptor] = True
            raw = raw.replace(b"\r\n", b"\n")
        return raw

    def write_descriptor(descriptor: int, raw: bytes) -> int:
        if descriptor not in text_descriptors:
            return os.write(descriptor, raw)
        translated = raw.replace(b"\n", b"\r\n")
        written = os.write(descriptor, translated)
        if written != len(translated):
            raise OSError("short physical write in CRT test seam")
        # CRT reports source bytes consumed, not expanded bytes written.
        return len(raw)

    seam = SimpleNamespace(**vars(os))
    seam.O_BINARY = binary
    seam.open = open_descriptor
    seam.read = read_descriptor
    seam.write = write_descriptor
    with patch.object(module, "os", seam):
        yield


# Intended cross-test export; the legacy private name remains the same callable.
crt_descriptor_io = _crt_descriptor_io


@contextmanager
def _project() -> Generator[Path, None, None]:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve() / "project"
        root.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        yield root


project = _project


def _passing_trace(root: Path) -> Mapping[str, object]:
    from test_browser_workflow_learning import browser_workflow_trace

    trace = write_browser_workflow_trace(browser_workflow_trace("click"), root)
    _ = approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))
    _ = replay_stored_browser_workflow_trace(root, str(trace["trace_id"]), {"fixture_id": "positive"})
    return read_browser_workflow_trace(root, str(trace["trace_id"]))


def _canonical(value: object) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

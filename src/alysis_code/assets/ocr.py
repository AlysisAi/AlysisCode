from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..command_text import command_text
from .models import OcrError

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScriptDetection:
    script: str
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    text: str
    languages_used: list[str]
    confidence: float
    engine_version: str
    elapsed_ms: int


class OcrProvider(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def installed_languages(self) -> list[str]: ...

    def detect_script(self, image_path: Path) -> ScriptDetection | None: ...

    def extract_text(
        self,
        image_path: Path,
        *,
        languages: list[str] | None = None,
    ) -> OcrResult: ...


class ScriptToLanguagesMapper:
    def __init__(self, registry: dict[str, list[str]] | None = None) -> None:
        self.registry = dict(_DEFAULT_SCRIPT_LANGUAGES)
        if registry:
            for script, languages in registry.items():
                self.registry[str(script)] = [str(item) for item in languages if str(item).strip()]

    def candidates(self, script: str, installed_languages: list[str]) -> list[str]:
        installed = set(installed_languages)
        candidates = self.registry.get(script, [])
        return [language for language in candidates if language in installed]


class TesseractOcrProvider:
    name = "tesseract"

    def __init__(
        self,
        *,
        binary: str = "tesseract",
        timeout_seconds: int = 30,
        script_mapper: ScriptToLanguagesMapper | None = None,
    ) -> None:
        self.binary = binary
        self.timeout_seconds = int(timeout_seconds)
        self.script_mapper = script_mapper or ScriptToLanguagesMapper()

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def installed_languages(self) -> list[str]:
        if not self.is_available():
            return []
        proc = self._run([self.binary, "--list-langs"])
        languages: list[str] = []
        for line in (proc.stdout or "").splitlines():
            clean = line.strip()
            clean_lower = clean.lower()
            if (
                not clean
                or clean_lower == "list"
                or clean_lower.startswith("list of available languages")
            ):
                continue
            languages.append(clean)
        return languages

    def detect_script(self, image_path: Path) -> ScriptDetection | None:
        if not self.is_available():
            return None
        proc = self._run(
            [
                self.binary,
                str(image_path),
                "-",
                "-l",
                "osd",
                "--psm",
                "0",
            ]
        )
        text = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
        script = _parse_label(text, "Script")
        if not script:
            return None
        confidence = _parse_float_label(text, "Script confidence") or 0.0
        return ScriptDetection(script=script, confidence=confidence)

    def extract_text(
        self,
        image_path: Path,
        *,
        languages: list[str] | None = None,
    ) -> OcrResult:
        if not self.is_available():
            raise OcrError("Tesseract binary is not available.")
        started = time.monotonic()
        selected_languages = self._resolve_languages(image_path, languages)
        language_arg = "+".join(selected_languages)
        proc = self._run([self.binary, str(image_path), "-", "-l", language_arg])
        confidence = self._extract_tsv_confidence(image_path, language_arg)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return OcrResult(
            text=proc.stdout or "",
            languages_used=selected_languages,
            confidence=confidence,
            engine_version=self._engine_version(),
            elapsed_ms=elapsed_ms,
        )

    def _resolve_languages(self, image_path: Path, languages: list[str] | None) -> list[str]:
        if languages:
            explicit = [item for item in languages if item.strip()]
            if explicit:
                return explicit
        try:
            installed = self.installed_languages()
        except OcrError as exc:
            LOGGER.warning("Tesseract language listing failed: %s", exc)
            installed = []
        try:
            detection = self.detect_script(image_path)
        except OcrError as exc:
            LOGGER.warning("Tesseract script detection failed: %s", exc)
            detection = None
        if detection is not None:
            mapped = self.script_mapper.candidates(detection.script, installed)
            if mapped:
                return mapped
        if installed:
            return installed
        return ["eng"]

    def _extract_tsv_confidence(self, image_path: Path, language_arg: str) -> float:
        try:
            proc = self._run([self.binary, str(image_path), "-", "-l", language_arg, "tsv"])
        except OcrError as exc:
            LOGGER.warning("Tesseract TSV confidence extraction failed: %s", exc)
            return 0.5
        confidences: list[float] = []
        for line in (proc.stdout or "").splitlines()[1:]:
            columns = line.split("\t")
            if len(columns) < 11:
                continue
            try:
                confidence = float(columns[10])
            except ValueError:
                continue
            if confidence >= 0:
                confidences.append(confidence)
        if not confidences:
            return 0.5
        return max(0.0, min(1.0, sum(confidences) / len(confidences) / 100.0))

    def _engine_version(self) -> str:
        try:
            proc = self._run([self.binary, "--version"])
        except OcrError:
            return "tesseract"
        first_line = (proc.stdout or "").splitlines()[0:1]
        return first_line[0].strip() if first_line else "tesseract"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                **command_text(),
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            LOGGER.warning("Tesseract command timed out after %ss.", self.timeout_seconds)
            raise OcrError(f"Tesseract timed out after {self.timeout_seconds}s.") from e
        except OSError as e:
            LOGGER.warning("Failed to run Tesseract: %s", e)
            raise OcrError(f"Failed to run Tesseract: {e}") from e
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            LOGGER.warning("Tesseract failed with exit code %s: %s", proc.returncode, stderr)
            raise OcrError(f"Tesseract failed with exit code {proc.returncode}: {stderr}")
        return proc


def _parse_label(text: str, label: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    return match.group("value").strip() if match else None


def _parse_float_label(text: str, label: str) -> float | None:
    value = _parse_label(text, label)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


_DEFAULT_SCRIPT_LANGUAGES: dict[str, list[str]] = {
    "Latin": ["eng", "fra", "deu", "spa", "ita", "por", "nld", "ron"],
    "Greek": ["ell", "grc"],
    "Cyrillic": ["rus", "ukr", "bul", "srp", "mkd"],
    "Arabic": ["ara", "fas", "urd"],
    "Han": ["chi_sim", "chi_tra"],
    "Hiragana": ["jpn"],
    "Katakana": ["jpn"],
    "Hangul": ["kor"],
    "Devanagari": ["hin", "san", "mar"],
}

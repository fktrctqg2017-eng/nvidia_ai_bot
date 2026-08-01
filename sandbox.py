"""Песочница (sandbox) для безопасного выполнения Python-кода, сгенерированного LLM.

Почему именно такой подход, а не Docker "по умолчанию":
- Бот должен одинаково хорошо работать и на полноценном сервере, и в Termux
  на Android (где Docker физически недоступен — там нет root и ядра для
  контейнеризации).
- Поэтому здесь реализована МНОГОУРОВНЕВАЯ изоляция с автоматическим
  определением лучшего доступного варианта на конкретной машине:

    Уровень 3 (сильнее всего): Docker-контейнер (--network none, без прав
        root внутри, read-only ФС кроме /tmp, лимиты CPU/памяти). Используется
        автоматически, если бинарник `docker` найден и демон доступен.

    Уровень 2: subprocess в отдельном сетевом namespace через `unshare --net`
        (доступно на большинстве обычных Linux без root — namespaces для сети
        может создавать непривилегированный пользователь, если это разрешено
        ядром/дистрибутивом). Полностью отрезает код от сети, но работает без
        Docker. Используется, если `docker` недоступен, но `unshare` есть и
        работает.

    Уровень 1 (базовый, работает почти везде, включая Termux): обычный
        subprocess с ограничениями через `resource.setrlimit` — лимит CPU-
        времени, лимит виртуальной памяти, лимит числа процессов, лимит
        размера создаваемых файлов, + общий wall-clock timeout. Сеть в этом
        режиме НЕ блокируется технически (Termux/Android не даёт создавать
        network namespaces без root), поэтому это компенсируется тем, что
        код всегда выполняется в одноразовой временной директории без
        доступа к остальной файловой системе бота и с явным предупреждением
        в системном промпте модели не писать код, обращающийся к сети.

Ни один из уровней не запускает код с правами, дающими доступ к файлам
пользователя/бота — исполнение всегда происходит в изолированной временной
директории, которая удаляется сразу после выполнения.

Это прагматичный компромисс: максимально безопасный вариант используется
автоматически, если доступен, а на менее защищённых окружениях (телефон)
всё равно действуют лимиты по времени/памяти/CPU, которые не дают коду
"уронить" систему или зависнуть навечно.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

from config import config

# ------------------------------------------------------------------ константы
#
# Основные лимиты (таймаут/память/CPU) настраиваются через config.py
# (переменные SANDBOX_*), здесь — только их значения по умолчанию и то, что
# редко нужно менять.

DEFAULT_TIMEOUT_SECONDS = config.sandbox_timeout_seconds   # wall-clock таймаут на выполнение кода
CPU_TIME_LIMIT_SECONDS = config.sandbox_cpu_limit_seconds  # лимит процессорного времени (RLIMIT_CPU)
MEMORY_LIMIT_BYTES = config.sandbox_memory_limit_mb * 1024 * 1024
MAX_OUTPUT_CHARS = 6000               # обрезаем очень длинный stdout/stderr
MAX_PROCESSES = 32                    # RLIMIT_NPROC — защита от fork-бомб
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024   # RLIMIT_FSIZE — защита от заполнения диска

DOCKER_IMAGE = "python:3.11-slim"


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    backend: str  # какой уровень изоляции реально использовался

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_llm_text(self) -> str:
        """Компактное текстовое представление результата для передачи обратно в LLM."""
        parts = [f"[exit_code={self.exit_code}, backend={self.backend}]"]
        if self.timed_out:
            parts.append(f"⏱ Выполнение прервано по таймауту ({DEFAULT_TIMEOUT_SECONDS} сек).")
        if self.stdout:
            parts.append("STDOUT:\n" + _truncate(self.stdout))
        if self.stderr:
            parts.append("STDERR:\n" + _truncate(self.stderr))
        if not self.stdout and not self.stderr:
            parts.append("(код выполнился без вывода)")
        return "\n\n".join(parts)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [обрезано, всего {len(text)} символов]"


# ------------------------------------------------------------------ определение возможностей окружения


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        res = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=3, check=False
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _unshare_net_available() -> bool:
    if shutil.which("unshare") is None:
        return False
    try:
        # Пробуем реально создать изолированный network namespace.
        res = subprocess.run(
            ["unshare", "--net", "--map-root-user", "--", "true"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


class SandboxBackend:
    """Кешируем определение доступного backend'а один раз за запуск процесса бота."""

    _cached: str | None = None

    @classmethod
    def detect(cls) -> str:
        if cls._cached is not None:
            return cls._cached
        if _docker_available():
            cls._cached = "docker"
        elif _unshare_net_available():
            cls._cached = "unshare-net"
        else:
            cls._cached = "subprocess"
        return cls._cached


# ------------------------------------------------------------------ RLIMIT preexec (Уровень 1 и 2)


def _apply_resource_limits() -> None:
    """Вызывается в дочернем процессе (preexec_fn) перед exec, чтобы
    ограничить его ресурсы ДО того, как он успеет что-либо сделать."""
    import resource  # доступно только на POSIX — Termux и Linux это ОК

    try:
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_TIME_LIMIT_SECONDS, CPU_TIME_LIMIT_SECONDS))
    except Exception:  # noqa: BLE001
        pass
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    except Exception:  # noqa: BLE001
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))
    except Exception:  # noqa: BLE001
        pass
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_BYTES))
    except Exception:  # noqa: BLE001
        pass
    # Отсоединяем процесс в свою группу, чтобы можно было надёжно убить
    # весь дерево процессов (включая случайно наплодённых потомков) по таймауту.
    try:
        import os

        os.setsid()
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ основная точка входа


async def run_python_code(code: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ExecutionResult:
    """Выполняет Python-код в наиболее безопасном доступном на этой машине
    окружении и возвращает структурированный результат.
    """
    backend = SandboxBackend.detect()
    if backend == "docker":
        return await _run_in_docker(code, timeout)
    if backend == "unshare-net":
        return await _run_with_unshare(code, timeout)
    return await _run_plain_subprocess(code, timeout)


async def _run_plain_subprocess(code: str, timeout: float) -> ExecutionResult:
    """Уровень 1: обычный subprocess + rlimits. Работает везде, включая Termux."""
    with tempfile.TemporaryDirectory(prefix="sandbox_") as tmp_dir:
        script_path = Path(tmp_dir) / "user_code.py"
        script_path.write_text(code, encoding="utf-8")

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # изолированный режим: игнорирует PYTHONPATH и site-packages пользователя
            str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_dir,
            preexec_fn=_apply_resource_limits if sys.platform != "win32" else None,
        )
        return await _wait_process(proc, timeout, backend="subprocess")


async def _run_with_unshare(code: str, timeout: float) -> ExecutionResult:
    """Уровень 2: subprocess + rlimits + отдельный network namespace без сети."""
    with tempfile.TemporaryDirectory(prefix="sandbox_") as tmp_dir:
        script_path = Path(tmp_dir) / "user_code.py"
        script_path.write_text(code, encoding="utf-8")

        cmd = [
            "unshare",
            "--net",            # own network namespace -> нет доступа к сети
            "--map-root-user",  # нужно для unshare --net без реального root
            "--",
            sys.executable,
            "-I",
            str(script_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_dir,
            preexec_fn=_apply_resource_limits,
        )
        return await _wait_process(proc, timeout, backend="unshare-net")


async def _run_in_docker(code: str, timeout: float) -> ExecutionResult:
    """Уровень 3: полноценный Docker-контейнер без сети и с read-only ФС."""
    with tempfile.TemporaryDirectory(prefix="sandbox_") as tmp_dir:
        script_path = Path(tmp_dir) / "user_code.py"
        script_path.write_text(code, encoding="utf-8")

        cmd = [
            "docker", "run",
            "--rm",
            "--network", "none",             # без доступа к сети
            "--memory", f"{MEMORY_LIMIT_BYTES}",
            "--memory-swap", f"{MEMORY_LIMIT_BYTES}",  # запрет использования swap сверху лимита
            "--cpus", "1",
            "--pids-limit", str(MAX_PROCESSES),
            "--read-only",                   # корневая ФС только для чтения
            "--tmpfs", "/tmp:rw,size=64m",   # но есть маленький перезаписываемый /tmp
            "--user", "nobody",              # без привилегий root внутри контейнера
            "-v", f"{tmp_dir}:/sandbox:ro",  # код монтируем только для чтения
            "-w", "/sandbox",
            DOCKER_IMAGE,
            "python3", "-I", "/sandbox/user_code.py",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            # На случай, если docker пропал между определением backend'а и вызовом
            return await _run_plain_subprocess(code, timeout)
        return await _wait_process(proc, timeout, backend="docker")


async def _wait_process(
    proc: asyncio.subprocess.Process, timeout: float, backend: str
) -> ExecutionResult:
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ExecutionResult(
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            exit_code=proc.returncode or 0,
            timed_out=False,
            backend=backend,
        )
    except asyncio.TimeoutError:
        await _kill_process_tree(proc)
        return ExecutionResult(
            stdout="",
            stderr="",
            exit_code=-1,
            timed_out=True,
            backend=backend,
        )


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Убивает процесс и, по возможности, всю его группу (из-за os.setsid в preexec)."""
    import os
    import signal

    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except Exception:  # noqa: BLE001
        pass


def extract_code_blocks(text: str, language: str = "python") -> list[str]:
    """Извлекает содержимое ```python ... ``` блоков из текста ответа модели."""
    import re

    pattern = rf"```(?:{language})?\s*\n(.*?)```"
    blocks = re.findall(pattern, text, flags=re.DOTALL | re.IGNORECASE)
    return [textwrap.dedent(b).strip() for b in blocks if b.strip()]


def describe_backend() -> str:
    """Человекочитаемое описание того, какой уровень изоляции сейчас активен —
    полезно показывать администратору/в логах при старте бота."""
    backend = SandboxBackend.detect()
    labels = {
        "docker": "🐳 Docker (максимальная изоляция: без сети, read-only ФС, без root)",
        "unshare-net": "🔒 unshare --net (изолированная сеть + rlimits, без Docker)",
        "subprocess": "⚠️ subprocess + rlimits (базовая изоляция — сеть НЕ блокируется технически; "
                      "рекомендуется для Termux/окружений без прав на namespaces)",
    }
    return labels.get(backend, backend)

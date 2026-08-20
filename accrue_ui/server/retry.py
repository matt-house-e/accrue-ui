"""Retry orchestration: import the pipeline + data, run one retry at a time.

The dashboard can only heal a run if it can rebuild the two objects the
original run was made of — the ``Pipeline`` and the input data. Neither is
in the run log (the log is events, not code), so the CLI takes import paths:

``--pipeline module:attr`` where *attr* is either

* a **Pipeline instance** — then ``--data module:attr`` is required too, and
  must name a DataFrame, a ``list[dict]``, or a zero-arg callable returning
  one; or
* a **zero-arg callable** returning ``(pipeline, data)`` — or
  ``(pipeline, data, config)`` when the run used a non-default
  :class:`~accrue.EnrichmentConfig` (a custom ``checkpoint_dir``, say).

Both specs are resolved **once, at server startup** (:meth:`RetryController.resolve`),
so ``GET /api/run``'s ``retry`` block tells the truth before anyone clicks:
either ``available: true``, or ``available: false`` with the precise reason
the import failed.

**Working directory.** ``retry_failed_async()`` reads the checkpoint the
failed run wrote, and a relative ``checkpoint_dir`` / ``cache_dir`` resolves
against the process cwd. The server is launched from the directory holding
``.accrue/`` (that is how it found the run log in the first place) and the
retry runs in-process, so both resolve exactly as they did for the run.

Exactly one retry runs at a time: :meth:`RetryController.start` refuses while
a task is in flight, and a task that raises records its exception in
:attr:`RetryController.last_error`, which rides the ``retry`` block back to
the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Reason reported when the server was launched without ``--pipeline``.
NO_PIPELINE = "launched without --pipeline"

#: Reason reported when a POST lands while a retry is already in flight.
ALREADY_RUNNING = "retry already running"


class _Unavailable(Exception):
    """Internal: carries the user-facing reason retry cannot be offered."""


@dataclass(frozen=True)
class _Target:
    """The resolved retry inputs: what to run, over what, with which config."""

    pipeline: Any
    data: Any
    config: Any


# ----------------------------------------------------------------- importing


def _describe(obj: Any) -> str:
    return type(obj).__name__


def _split_spec(spec: str, flag: str) -> tuple[str, str]:
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise _Unavailable(f"{flag} {spec!r} is not module:attr")
    return module_name, attr


def _import_attr(spec: str, flag: str) -> Any:
    """Import ``module:attr`` (dotted attrs allowed), or explain the failure."""
    module_name, attr_path = _split_spec(spec, flag)
    try:
        obj: Any = importlib.import_module(module_name)
    except Exception as exc:  # the user's module runs on import; anything goes
        raise _Unavailable(
            f"{flag} {spec!r}: cannot import module {module_name!r} "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    seen = module_name
    for part in attr_path.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise _Unavailable(
                f"{flag} {spec!r}: {seen!r} has no attribute {part!r}"
            ) from exc
        seen = f"{seen}.{part}"
    return obj


def _call_zero_arg(func: Any, spec: str, flag: str, label: str) -> Any:
    try:
        return func()
    except Exception as exc:
        raise _Unavailable(
            f"{flag} {spec!r}: calling {label}() raised {type(exc).__name__}: {exc}"
        ) from exc


def _is_dataframe(obj: Any) -> bool:
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas ships with accrue
        return False
    return isinstance(obj, pd.DataFrame)


def _check_data(obj: Any, message: str) -> Any:
    """A DataFrame or a ``list[dict]``, else :class:`_Unavailable`."""
    if _is_dataframe(obj):
        return obj
    if isinstance(obj, list) and all(isinstance(item, dict) for item in obj):
        return obj
    raise _Unavailable(message.format(what=_describe(obj)))


def _is_pipeline(obj: Any) -> bool:
    """Duck-typed: anything that can run a failed-only retry qualifies."""
    return hasattr(obj, "retry_failed_async")


def _default_config() -> Any:
    """``EnrichmentConfig(enable_checkpointing=True)``, no progress bar."""
    try:
        from accrue import EnrichmentConfig
    except Exception as exc:  # accrue is a hard dependency; be explicit anyway
        raise _Unavailable(
            f"accrue is not importable ({type(exc).__name__}: {exc})"
        ) from exc
    return EnrichmentConfig(enable_checkpointing=True, enable_progress_bar=False)


def _with_checkpointing(config: Any) -> Any:
    """Retry reads the checkpoint, so checkpointing is non-negotiable."""
    if getattr(config, "enable_checkpointing", False):
        return config
    from dataclasses import replace

    return replace(config, enable_checkpointing=True)


def resolve_target(pipeline_spec: str | None, data_spec: str | None) -> _Target:
    """Import both specs and validate the shapes; raises :class:`_Unavailable`."""
    if pipeline_spec is None:
        if data_spec is not None:
            raise _Unavailable("--data requires --pipeline")
        raise _Unavailable(NO_PIPELINE)

    attr = _import_attr(pipeline_spec, "--pipeline")

    if _is_pipeline(attr):
        if data_spec is None:
            raise _Unavailable(
                f"--pipeline {pipeline_spec!r} is a {_describe(attr)}; "
                "retry also needs --data module:attr"
            )
        data = _import_attr(data_spec, "--data")
        if callable(data) and not _is_dataframe(data):
            label = data_spec.partition(":")[2]
            data = _call_zero_arg(data, data_spec, "--data", label)
            data = _check_data(
                data,
                f"--data {data_spec!r}: {label}() returned {{what}}; expected a "
                "DataFrame or a list of dicts",
            )
        else:
            data = _check_data(
                data,
                f"--data {data_spec!r} is a {{what}}; expected a DataFrame, a "
                "list of dicts, or a zero-arg callable returning one",
            )
        return _Target(attr, data, _default_config())

    if callable(attr):
        if data_spec is not None:
            raise _Unavailable(
                f"--pipeline {pipeline_spec!r} already supplies data; drop --data"
            )
        label = pipeline_spec.partition(":")[2]
        produced = _call_zero_arg(attr, pipeline_spec, "--pipeline", label)
        if not isinstance(produced, tuple) or not 2 <= len(produced) <= 3:
            raise _Unavailable(
                f"--pipeline {pipeline_spec!r}: {label}() returned "
                f"{_describe(produced)}; expected (pipeline, data)"
            )
        pipeline, data = produced[0], produced[1]
        config = produced[2] if len(produced) == 3 else None
        if not _is_pipeline(pipeline):
            raise _Unavailable(
                f"--pipeline {pipeline_spec!r}: {label}() returned "
                f"{_describe(pipeline)} as its pipeline; expected a Pipeline"
            )
        data = _check_data(
            data,
            f"--pipeline {pipeline_spec!r}: {label}() returned {{what}} as its "
            "data; expected a DataFrame or a list of dicts",
        )
        config = _default_config() if config is None else _with_checkpointing(config)
        return _Target(pipeline, data, config)

    raise _Unavailable(
        f"--pipeline {pipeline_spec!r} is a {_describe(attr)}; expected a "
        "Pipeline or a zero-arg callable returning (pipeline, data)"
    )


# ---------------------------------------------------------------- controller


class RetryController:
    """Owns the resolved retry target and the single in-flight retry task."""

    def __init__(
        self,
        run_path: str | Path,
        pipeline_spec: str | None = None,
        data_spec: str | None = None,
    ) -> None:
        self.run_path = Path(run_path)
        self.pipeline_spec = pipeline_spec
        self.data_spec = data_spec
        self.last_error: str | None = None
        self._resolved = False
        self._target: _Target | None = None
        self._reason: str | None = NO_PIPELINE
        self._task: asyncio.Task | None = None

    # -- resolution --------------------------------------------------------

    def resolve(self) -> None:
        """Import the specs once; failures become :attr:`reason`, never raise."""
        if self._resolved:
            return
        self._resolved = True
        try:
            self._target = resolve_target(self.pipeline_spec, self.data_spec)
            self._reason = None
        except _Unavailable as exc:
            self._target = None
            self._reason = str(exc)

    @property
    def available(self) -> bool:
        self.resolve()
        return self._target is not None

    @property
    def reason(self) -> str | None:
        self.resolve()
        return self._reason

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def resume_command(self) -> str:
        """The command line that reproduces this server, retry included."""
        spec = (
            shlex.quote(self.pipeline_spec) if self.pipeline_spec else "<module:attr>"
        )
        parts = ["accrue-ui", shlex.quote(str(self.run_path)), "--pipeline", spec]
        if self.data_spec:
            parts += ["--data", shlex.quote(self.data_spec)]
        return " ".join(parts)

    def block(self) -> dict[str, Any]:
        """The ``retry`` object of ``GET /api/run`` (docs/api-shapes.md)."""
        return {
            "available": self.available,
            "reason": self.reason,
            "resume_command": self.resume_command,
            "running": self.running,
            "last_error": self.last_error,
        }

    # -- running -----------------------------------------------------------

    def start(
        self,
        rows: list[int],
        *,
        steps: list[str] | None = None,
        display_key: str | None = None,
    ) -> bool:
        """Launch a retry of *rows*; False when one is already in flight.

        Returns immediately — the retry runs as a background task and the
        healed cells arrive through the run log like any other records.
        """
        if self.running:
            return False
        self.last_error = None
        self._task = asyncio.create_task(
            self._run(list(rows), steps, display_key), name="accrue-ui-retry"
        )
        return True

    async def _run(
        self, rows: list[int], steps: list[str] | None, display_key: str | None
    ) -> None:
        target = self._target
        if target is None:  # pragma: no cover - start() is guarded by available
            self.last_error = self._reason
            return
        try:
            await target.pipeline.retry_failed_async(
                target.data,
                config=target.config,
                rows=rows,
                steps=steps,
                run_log=str(self.run_path),
                display_key=display_key,
            )
        except asyncio.CancelledError:
            self.last_error = "retry cancelled"
            raise
        except Exception as exc:
            # A user pipeline can raise anything; surface it instead of
            # letting the task die silently (retry.last_error in /api/run).
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("retry failed")

    async def aclose(self) -> None:
        """Cancel an in-flight retry (server shutdown)."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

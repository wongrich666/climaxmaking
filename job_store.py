from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobState:
    job_id: str
    status: str = "queued"
    title: str = ""
    provider: str = ""
    episode_count: int = 0
    completed_count: int = 0
    passed_audit_count: int = 0
    fallback_count: int = 0
    download_name: str = ""
    content: str = ""
    audits: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    partial_output_path: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "title": self.title,
            "provider": self.provider,
            "episode_count": self.episode_count,
            "completed_count": self.completed_count,
            "passed_audit_count": self.passed_audit_count,
            "fallback_count": self.fallback_count,
            "download_name": self.download_name,
            "content": self.content,
            "audits": self.audits,
            "error": self.error,
            "partial_output_path": self.partial_output_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()

    def create_job(self, job_id: str, **fields: Any) -> JobState:
        with self._lock:
            state = JobState(job_id=job_id)
            self._apply_fields(state, fields)
            self._jobs[job_id] = state
            return self._clone(state)

    def update_job(self, job_id: str, **fields: Any) -> JobState:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"job not found: {job_id}")
            state = self._jobs[job_id]
            self._apply_fields(state, fields)
            return self._clone(state)

    def get_job(self, job_id: str) -> JobState | None:
        with self._lock:
            state = self._jobs.get(job_id)
            return self._clone(state) if state else None

    @staticmethod
    def _apply_fields(state: JobState, fields: dict[str, Any]) -> None:
        for key, value in fields.items():
            setattr(state, key, value)
        state.updated_at = time.time()

    @staticmethod
    def _clone(state: JobState | None) -> JobState | None:
        if state is None:
            return None
        return JobState(
            job_id=state.job_id,
            status=state.status,
            title=state.title,
            provider=state.provider,
            episode_count=state.episode_count,
            completed_count=state.completed_count,
            passed_audit_count=state.passed_audit_count,
            fallback_count=state.fallback_count,
            download_name=state.download_name,
            content=state.content,
            audits=list(state.audits),
            error=state.error,
            partial_output_path=state.partial_output_path,
            created_at=state.created_at,
            updated_at=state.updated_at,
        )

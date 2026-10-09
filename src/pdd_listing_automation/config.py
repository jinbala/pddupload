from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    root_dir: Path
    data_dir: Path
    profile_dir: Path
    artifacts_dir: Path
    database_path: Path
    headless: bool = False
    slow_mo_ms: int = 0
    login_wait_seconds: int = 300

    @classmethod
    def from_environment(cls) -> Settings:
        root_dir = Path(os.getenv("PDD_AUTOMATION_HOME", Path.cwd())).resolve()
        data_dir = root_dir / "data"
        artifacts_dir = root_dir / "artifacts"
        return cls(
            root_dir=root_dir,
            data_dir=data_dir,
            profile_dir=data_dir / "browser-profile",
            artifacts_dir=artifacts_dir,
            database_path=data_dir / "automation.sqlite3",
            headless=os.getenv("PDD_AUTOMATION_HEADLESS", "false").lower() == "true",
            slow_mo_ms=int(os.getenv("PDD_AUTOMATION_SLOW_MO_MS", "0")),
            login_wait_seconds=int(os.getenv("PDD_AUTOMATION_LOGIN_WAIT_SECONDS", "300")),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

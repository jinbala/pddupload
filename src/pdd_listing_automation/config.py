from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


def _load_dotenv(directory: Path) -> None:
    """极简 .env 加载（不依赖 python-dotenv）：只读 KEY=VALUE 行，不覆盖已存在的环境变量。

    .env 被 .gitignore 忽略，适合放 headless、店铺名等本地配置，一次写好后续都生效。
    """
    env_path = directory / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
        cwd = Path.cwd()
        _load_dotenv(cwd)
        root_dir = Path(os.getenv("PDD_AUTOMATION_HOME", cwd)).resolve()
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

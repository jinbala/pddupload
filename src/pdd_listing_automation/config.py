from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# 源码运行使用项目目录；安装为普通包时使用固定用户目录，均不依赖启动位置。
_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT_DIR = (
    _PACKAGE_DIR.parent.parent if _PACKAGE_DIR.parent.name == "src"
    else Path.home() / ".pdd-listing-automation"
)


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
    slow_mo_ms: int = Field(default=0, ge=0)
    login_wait_seconds: int = Field(default=300, ge=0)

    @classmethod
    def from_environment(cls) -> Settings:
        _load_dotenv(DEFAULT_ROOT_DIR)
        configured_home = os.getenv("PDD_AUTOMATION_HOME", "").strip()
        root_dir = Path(configured_home).expanduser() if configured_home else DEFAULT_ROOT_DIR
        if not root_dir.is_absolute():
            root_dir = DEFAULT_ROOT_DIR / root_dir
        root_dir = root_dir.resolve()
        if root_dir != DEFAULT_ROOT_DIR:
            _load_dotenv(root_dir)
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

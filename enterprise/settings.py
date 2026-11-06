import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env():
    """Local development config; existing process environment takes precedence."""
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT / "data" / "tasks.sqlite3"
    home: Path = ROOT / "data" / "harness"
    sales_url: str = "http://127.0.0.1:8091"
    mode: str = "live"
    app_mode: str = "normal"
    acceptance_db: Path | None = None
    acceptance_root: Path | None = None
    trace_dir: Path | None = None

    extra_patches: tuple[str, ...] = ()

    @property
    def isolated(self):
        return self.app_mode in {'acceptance', 'sandbox'}

    @property
    def acceptance(self):
        return self.app_mode == 'acceptance'

    @property
    def cookie_name(self):
        return 'enterprise_sandbox_session' if self.app_mode == 'sandbox' else 'enterprise_acceptance_session' if self.acceptance else 'enterprise_session'

    def require_execution(self):
        if self.acceptance:
            from fastapi import HTTPException
            raise HTTPException(409, '安全验收模式：后台 Agent 执行已禁用')

    def checked(self):
        if self.app_mode not in {'normal', 'acceptance', 'sandbox'}:
            raise ValueError('APP_MODE must be normal or acceptance')
        if self.isolated:
            from dataclasses import replace
            from .acceptance import validate_environment
            path, root = validate_environment(self.acceptance_db, self.acceptance_root, self.db_path)
            if self.app_mode == 'sandbox':
                from urllib.parse import urlparse
                endpoint = urlparse(self.sales_url)
                if endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.2' or not endpoint.port or endpoint.port == 8091:
                    raise ValueError('独立执行环境必须显式配置127.0.0.2测试端口')
                if not self.extra_patches or any(not Path(p).resolve().is_relative_to(root) or not Path(p).is_file() for p in self.extra_patches):
                    raise ValueError('独立执行环境必须提供隔离目录内的模型配置')
                if self.trace_dir is None or not self.trace_dir.resolve().is_relative_to(root):
                    raise ValueError('独立执行环境trace必须保存在隔离目录')
            return replace(self, db_path=path, acceptance_root=root, home=root/'harness')
        return self

    @classmethod
    def from_env(cls):
        # Acceptance never loads the production .env (which can contain secrets).
        if os.getenv('APP_MODE', 'normal') not in {'acceptance','sandbox'}:
            load_env()
        mode = os.getenv("AGENT_MODE", "live")
        if mode not in {"live", "mock"}:
            raise ValueError("AGENT_MODE must be live or mock")
        return cls(
            db_path=Path(os.getenv("TASK_DB", str(cls.db_path))).resolve(),
            home=Path(os.getenv("HARNESS_HOME", str(cls.home))).resolve(),
            sales_url=os.getenv("SALES_API_URL", cls.sales_url),
            mode=mode,
            app_mode=os.getenv('APP_MODE', 'normal'),
            acceptance_db=Path(os.environ['ACCEPTANCE_DB']) if os.getenv('ACCEPTANCE_DB') else None,
            acceptance_root=Path(os.environ['ACCEPTANCE_ROOT']) if os.getenv('ACCEPTANCE_ROOT') else None,
        )

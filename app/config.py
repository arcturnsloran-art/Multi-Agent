"""只在服务端读取配置，不向进程环境注入值，也不输出配置对象。"""
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    """SecretStr 防止调试 repr 意外显示密钥；网络认证时才显式取值。"""
    model_config = ConfigDict(frozen=True)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    model: str = "deepseek-flash"

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value().strip())


def load_settings(env_path: Path | None = None) -> Settings:
    """固定从项目 .env 读取；关闭变量插值，避免密钥中的 $ 被替换。"""
    values = dotenv_values(env_path or ROOT / ".env", encoding="utf-8-sig", interpolate=False)
    return Settings(api_key=SecretStr((values.get("DEEPSEEK_API_KEY") or "").strip()),
                    model=(values.get("DEEPSEEK_MODEL") or "deepseek-flash").strip())

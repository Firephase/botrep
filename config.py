import os
from dataclasses import dataclass, field


@dataclass
class Config:
    telegram_token: str
    yougile_api_key: str
    yougile_board_id: str
    yougile_project_key: str
    db_path: str
    log_level: str
    allowed_chat_ids: list[int]
    large_range_limit: int
    qwen_api_key: str = ""
    qwen_model: str = "qwen-plus"
    groq_api_key: str = ""
    groq_model: str = "whisper-large-v3-turbo"
    groq_proxy: str = ""
    qwen_proxy: str = ""
    report_chat_id: int = 0
    report_time: str = "18:00"
    silent_mode: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        raw = os.getenv("ALLOWED_CHAT_IDS", "")
        chat_ids = [int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            yougile_api_key=os.getenv("YOUGILE_API_KEY", ""),
            yougile_board_id=os.getenv("YOUGILE_BOARD_ID", ""),
            yougile_project_key=os.getenv("YOUGILE_PROJECT_KEY", "default"),
            db_path=os.getenv("DB_PATH", "data/bot.db"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            allowed_chat_ids=chat_ids,
            large_range_limit=int(os.getenv("LARGE_RANGE_LIMIT", "50")),
            qwen_api_key=os.getenv("QWEN_API_KEY", ""),
            qwen_model=os.getenv("QWEN_MODEL", "qwen-plus"),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            groq_model=os.getenv("GROQ_MODEL", "whisper-large-v3-turbo"),
            groq_proxy=os.getenv("GROQ_PROXY", ""),
            qwen_proxy=os.getenv("QWEN_PROXY", ""),
            report_chat_id=int(os.getenv("REPORT_CHAT_ID", "0") or "0"),
            report_time=os.getenv("REPORT_TIME", "18:00"),
            silent_mode=os.getenv("SILENT_MODE", "").lower() in ("1", "true", "yes"),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, val in [
                ("TELEGRAM_BOT_TOKEN", self.telegram_token),
                ("YOUGILE_API_KEY", self.yougile_api_key),
                ("YOUGILE_BOARD_ID", self.yougile_board_id),
            ]
            if not val
        ]
        if missing:
            raise ValueError("Не заданы переменные окружения: " + ", ".join(missing))

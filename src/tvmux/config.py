"""Configuration management for tvmux."""
import os
import tomllib
from pathlib import Path
from typing import Optional, Dict, Any

from pydantic import BaseModel, Field
import tomli_w


class OutputConfig(BaseModel):
    """Output configuration."""
    directory: str = Field(default="~/Videos/tmux", description="Base directory for recordings")
    date_format: str = Field(default="%Y-%m", description="Date format for subdirectories")


class ServerConfig(BaseModel):
    """Server configuration."""
    port: int = Field(default=21590, description="Server port")
    auto_start: bool = Field(default=True, description="Auto-start server when needed")
    auto_shutdown: bool = Field(default=True, description="Auto-shutdown when no recordings")


class RecordingConfig(BaseModel):
    """Recording configuration."""
    repair_on_stop: bool = Field(default=True, description="Repair cast files on stop")
    follow_active_pane: bool = Field(default=True, description="Follow active pane switches")


class AnnotationConfig(BaseModel):
    """Annotation configuration."""
    include_cursor_state: bool = Field(default=True, description="Include cursor position/visibility")


class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: str = Field(default="INFO", description="Log level (DEBUG/INFO/WARNING/ERROR)")
    include_access_logs: bool = Field(default=False, description="Include HTTP access logs in server")
    client_log_file: Optional[str] = Field(default="~/.tvmux/client.log", description="Client log file path (None = no file logging)")


class Config(BaseModel):
    """Main tvmux configuration."""
    output: OutputConfig = Field(default_factory=OutputConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    annotations: AnnotationConfig = Field(default_factory=AnnotationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(config_file: Optional[str] = None) -> Config:
    """Load configuration from file and environment variables.

    Precedence (highest to lowest):
    1. Environment variables
    2. Specified config file
    3. TVMUX_CONFIG_FILE environment variable
    4. ~/.tvmux.conf
    5. Built-in defaults
    """
    config_data = {}

    # Find config file
    if config_file:
        config_path = Path(config_file).expanduser()
    elif os.getenv("TVMUX_CONFIG_FILE"):
        config_path = Path(os.getenv("TVMUX_CONFIG_FILE")).expanduser()
    else:
        config_path = Path.home() / ".tvmux.conf"

    # Load from file if it exists
    if config_path.exists():
        with open(config_path, "rb") as f:
            config_data = tomllib.load(f)

    # Apply environment variable overrides programmatically
    env_overrides = load_all_env_overrides()

    # Merge environment overrides into config data
    for section, values in env_overrides.items():
        config_data.setdefault(section, {}).update(values)

    # Create and return config object
    return Config(**config_data)


def generate_env_var_name(section: str, field: str) -> str:
    """Generate environment variable name for a config field.

    Follows convention: TVMUX_{SECTION}_{FIELD}
    """
    return f"TVMUX_{section.upper()}_{field.upper()}"


def get_all_env_mappings() -> Dict[str, tuple[str, str]]:
    """Get all possible environment variable mappings.

    Returns dict mapping env var name to (section, field) tuple.
    """
    mappings = {}

    # Get the default config to introspect fields
    default_config = Config()

    for section_name, section_obj in default_config.model_dump().items():
        if isinstance(section_obj, dict):
            for field_name in section_obj.keys():
                env_var = generate_env_var_name(section_name, field_name)
                mappings[env_var] = (section_name, field_name)

    return mappings


def load_all_env_overrides() -> Dict[str, Any]:
    """Load all environment variable overrides programmatically."""
    env_overrides = {}

    for env_var, (section, field) in get_all_env_mappings().items():
        if env_value := os.getenv(env_var):
            env_overrides.setdefault(section, {})[field] = _convert_env_value(env_value)

    return env_overrides


def _convert_env_value(value: str) -> Any:
    """Convert environment variable string to appropriate type."""
    # Handle boolean values
    if value.lower() in ("true", "1", "yes", "on"):
        return True
    elif value.lower() in ("false", "0", "no", "off"):
        return False

    # Try to convert to int
    try:
        return int(value)
    except ValueError:
        pass

    # Return as string
    return value


def dump_config_toml(config: Config) -> str:
    """Convert Config to TOML string."""
    return tomli_w.dumps(config.model_dump())


def dump_config_env(config: Config) -> str:
    """Convert Config to environment variable format."""
    lines = []
    config_dict = config.model_dump()

    for section_name, section_data in config_dict.items():
        if isinstance(section_data, dict):
            for field_name, value in section_data.items():
                env_var = generate_env_var_name(section_name, field_name)
                # Format boolean values appropriately for shell
                if isinstance(value, bool):
                    value = "true" if value else "false"
                lines.append(f"{env_var}={value}")

    return "\n".join(lines)


# Global config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config: Config) -> None:
    """Set the global configuration instance."""
    global _config
    _config = config

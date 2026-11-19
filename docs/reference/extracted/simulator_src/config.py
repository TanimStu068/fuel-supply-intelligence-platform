from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Scenario files are baked into the Docker image at /app/scenarios/ at build
# time. The simulator always loads the active scenario from this fixed location
# inside the container — it cannot be changed at runtime via env vars. This
# guarantees every participant runs the identical scenario for fair judging.
BUILTIN_SCENARIOS_DIR = Path("/app/scenarios")
BUILTIN_SCENARIO_FILE = BUILTIN_SCENARIOS_DIR / "baseline.yaml"


def _resolve_scenario_file() -> Path:
    """Pick the active scenario file.

    Production (Docker): use the file baked into the image at /app/scenarios/.
    Local development/tests: fall back to the sibling ./scenarios/ directory
    so that running pytest outside a container still works.
    """
    if BUILTIN_SCENARIO_FILE.exists():
        return BUILTIN_SCENARIO_FILE
    local = Path(__file__).resolve().parent.parent / "scenarios" / "baseline.yaml"
    return local


SCENARIO_FILE = str(_resolve_scenario_file())


class Settings(BaseSettings):
    database_url: str = "sqlite:////app/data/fuel_simulator.db"
    simulator_start_mode: str = "paused"
    # SIMULATION_SPEED: how many ticks per second of wall-clock time fire while
    # the simulator is in RUNNING mode. Each tick advances simulation_time by
    # `tick_minutes`. The sleep between ticks is clamped to [0.05s, 10s] so
    # extreme values cannot burn CPU or stall the runner.
    simulation_speed: float = 8.0
    # TICK_MINUTES: how many simulated minutes pass per tick. With the
    # defaults (speed=8, tick_minutes=15) the sim advances 2 simulated hours
    # per real second. Larger values = coarser time steps (faster perceived
    # time) but demand/allocation rounding happens in larger chunks.
    tick_minutes: int = 15
    log_level: str = "INFO"
    model_config = SettingsConfigDict(extra="ignore")

settings = Settings()

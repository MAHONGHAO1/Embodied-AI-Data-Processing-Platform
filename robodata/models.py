from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class Episode:
    episode_index: int
    table: pd.DataFrame
    metadata: dict[str, Any]
    video_path: Path
    data_path: Path
    field_names: list[str]
    fps: float
    source: dict[str, Any]
    expected_length: int
    video_start_time: float = 0.0
    video_end_time: float | None = None

    def names_for(self, field: str) -> list[str]:
        feature = self.metadata.get('info', {}).get('features', {}).get(field, {})
        names = feature.get('names')
        if isinstance(names, dict):
            names = names.get('motors', next(iter(names.values()), None))
        if isinstance(names, list) and all(isinstance(x, str) for x in names):
            return names
        shape = feature.get('shape', [len(self.field_names)])
        dimension = int(shape[0]) if shape else len(self.field_names)
        if len(self.field_names) == dimension:
            return self.field_names
        return [f'{field}[{i}]' for i in range(dimension)]

    @property
    def state_names(self) -> list[str]:
        return self.names_for('observation.state')

    @property
    def action_names(self) -> list[str]:
        return self.names_for('action')

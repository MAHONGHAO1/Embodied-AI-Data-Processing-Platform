"""Fix LeRobot's uint8 video statistics without forking the package.

``RunningQuantileStats.update`` computes ``batch**2``. When the batch is
``uint8`` (LeRobot samples video frames as uint8 to save memory), 255**2 wraps
and the variance is clipped to zero.

The patch keeps the integer batch for min/max/histograms and computes only the
mean of squares in float64. Casting the whole batch to float64 also fixes the
overflow, but makes the per-frame histogram path much slower; this version
preserves the original hot path.
"""

from __future__ import annotations


def _integer_safe_update(original):
    def update(self, batch):
        import numpy as np

        array = np.asarray(batch)
        if not np.issubdtype(array.dtype, np.integer):
            return original(self, array)
        values = array.reshape(-1, array.shape[-1])
        num_elements, vector_length = values.shape
        if self._count == 0:
            self._mean = np.mean(values, axis=0)
            self._mean_of_squares = np.mean(values.astype(np.float64) ** 2, axis=0)
            self._min = np.min(values, axis=0)
            self._max = np.max(values, axis=0)
            self._histograms = [np.zeros(self._num_quantile_bins) for _ in range(vector_length)]
            self._bin_edges = [
                np.linspace(self._min[i] - 1e-10, self._max[i] + 1e-10, self._num_quantile_bins + 1)
                for i in range(vector_length)
            ]
        else:
            if vector_length != self._mean.size:
                raise ValueError("The length of new vectors does not match the initialized vector length.")
            new_max = np.max(values, axis=0)
            new_min = np.min(values, axis=0)
            max_changed = np.any(new_max > self._max)
            min_changed = np.any(new_min < self._min)
            self._max = np.maximum(self._max, new_max)
            self._min = np.minimum(self._min, new_min)
            if max_changed or min_changed:
                self._adjust_histograms()
        self._count += num_elements
        batch_mean = np.mean(values, axis=0)
        batch_mean_of_squares = np.mean(values.astype(np.float64) ** 2, axis=0)
        self._mean += (batch_mean - self._mean) * (num_elements / self._count)
        self._mean_of_squares += (batch_mean_of_squares - self._mean_of_squares) * (
            num_elements / self._count)
        self._update_histograms(values)

    update._robodata_integer_safe = True
    update.__wrapped__ = original
    return update


def patch_running_quantile_stats() -> bool:
    """Return True when the patch is active; False if LeRobot is unavailable."""
    try:
        from lerobot.datasets.compute_stats import RunningQuantileStats
    except Exception:
        return False
    current = RunningQuantileStats.update
    if getattr(current, "_robodata_integer_safe", False):
        return True
    RunningQuantileStats.update = _integer_safe_update(current)
    return True

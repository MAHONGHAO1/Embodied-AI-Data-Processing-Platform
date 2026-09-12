"""Shared spacing for the business and technical signal previews."""

from __future__ import annotations

import plotly.graph_objects as go


def apply_signal_layout(figure: go.Figure, *, x_title: str) -> None:
    """Keep the legend above the plot and reserve the bottom for axis labels.

    The heading belongs to the surrounding Streamlit layout, not the SVG.
    Plotly can expand the margins when long channel names wrap on narrow screens.
    """
    figure.update_layout(
        height=400,
        autosize=True,
        margin={"l": 60, "r": 20, "t": 96, "b": 72, "autoexpand": True},
        paper_bgcolor="#ffffff", plot_bgcolor="#ffffff", hovermode="x unified",
        font={"family": "Arial, Microsoft YaHei, sans-serif", "color": "#475569"},
        legend={
            "orientation": "h", "y": 1.08, "yanchor": "bottom",
            "x": 0, "xanchor": "left", "font": {"size": 11},
        },
        xaxis={
            "title": {"text": x_title, "standoff": 12}, "automargin": True,
            "gridcolor": "#e2e8f0", "zeroline": False,
        },
        yaxis={
            "title": {"text": "原始值", "standoff": 10}, "automargin": True,
            "gridcolor": "#edf2f7", "zeroline": False,
        },
    )

"""
title: Toy Visualization Data
author: Classic298
version: 1.0.0
required_open_webui_version: 0.10.2
description: Returns a small deterministic dataset for testing Inline Visualizer — Tool Result.
"""


from typing import Literal


class Tools:
    async def sample_market_series(
        self,
        shape: Literal["line", "bar"] = "line",
    ) -> dict:
        """Return a small deterministic time series for visualization tests.

        After this call completes, pass this call's exact tool_call_id/call_id
        to visualize_tool_result. Do not call this function a second time.

        :param shape: Preferred chart shape for the visualization.
        :return: Structured categories and numeric values suitable for a line or bar chart.
        """
        return {
            "title": "Toy market series",
            "shape": shape,
            "unit": "%",
            "series": [
                {"period": "2024-Q1", "value": 7.5},
                {"period": "2024-Q2", "value": 9.0},
                {"period": "2024-Q3", "value": 8.25},
                {"period": "2024-Q4", "value": 11.0},
                {"period": "2025-Q1", "value": 10.5},
                {"period": "2025-Q2", "value": 13.0},
            ],
        }

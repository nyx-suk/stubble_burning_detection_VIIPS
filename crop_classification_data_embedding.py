"""Compatibility launcher for the relocated analysis script."""

from pathlib import Path
from runpy import run_path

if __name__ == "__main__":
    run_path(str(Path(__file__).parent / "scripts" / "crop_classification_data_embedding.py"), run_name="__main__")

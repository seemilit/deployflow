"""Development launcher for the PySide6 interface."""

import sys
from pathlib import Path

application_directory = str(Path(__file__).resolve().parent)
if application_directory not in sys.path:
    sys.path.insert(0, application_directory)

from application_qt import main


if __name__ == "__main__":
    main()

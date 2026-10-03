import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from car_host.runtime import prepare_qt_runtime

prepare_qt_runtime()


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app

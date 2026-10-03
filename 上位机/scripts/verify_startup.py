"""Exercise the formal entrypoint with a fake byte link, never real Bluetooth."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from car_host import __main__ as entry
from car_host.controller import HostController
from car_host.preferences import PreferencesStore
from car_host.project_protocol import HELLO
from car_host.ui import MainWindow
from PySide6.QtCore import QTimer
from verify_project_gui import ReadOnlyFixture


def main():
    root = Path(__file__).resolve().parents[1]
    isolated_path = root / ".qa" / "startup" / str(time.time_ns()) / "preferences.json"
    store = PreferencesStore(isolated_path)
    links = []
    failures = []
    disabled = "--no-auto-connect" in sys.argv

    class TrackedFixture(ReadOnlyFixture):
        def __init__(self, config):
            super().__init__(config)
            self.sent = []

        def request_send(self, payload, request_id=""):
            self.sent.append(payload)
            return super().request_send(payload, request_id)

    def make_link(config):
        link = TrackedFixture(config)
        links.append((config, link))
        return link

    class Window(MainWindow):
        def show(self):
            super().show()
            QTimer.singleShot(850, self.inspect_and_close)

        def inspect_and_close(self):
            try:
                assert store.path.exists()
                assert store.load().preferences.auto_connect  # CLI does not change saved preference.
                if disabled:
                    assert not links and not self.startup_connector.attempted
                else:
                    assert len(links) == 1 and self.startup_connector.attempted
                    config, link = links[0]
                    assert config.kind == "bluetooth_spp"
                    assert config.bluetooth_address == "2A:A2:19:07:1B:8B"
                    assert config.protocol == "PROJECT_V1"
                    assert len(link.sent) == 1 and link.sent[0][3] == HELLO
                    assert self.controller.adapter.seq == 1
                    assert link.seq == 1 and link.session
                assert self.controller.actual_parameters is None
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
            self.close()

    entry.HostController = lambda path: HostController(path, worker_factory=make_link)
    entry.PreferencesStore = lambda path: store
    entry.MainWindow = Window
    result = entry.main()
    if failures:
        raise RuntimeError("; ".join(failures))
    print(f"Formal startup OK: auto_connect={not disabled}, fake_links={len(links)}; no hardware")
    return result


if __name__ == "__main__":
    raise SystemExit(main())

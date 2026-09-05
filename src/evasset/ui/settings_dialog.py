"""Application settings: ESI application credentials and pricing rules."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..logsetup import LOG_PATH, configure


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.resize(620, 620)

        root = QVBoxLayout(self)

        # --- ESI application
        esi_box = QGroupBox("ESI application")
        esi = QFormLayout(esi_box)
        self.client_id = QLineEdit(settings.client_id)
        self.client_secret = QLineEdit(settings.client_secret)
        self.client_secret.setEchoMode(QLineEdit.Password)
        self.client_secret.setPlaceholderText("leave blank to use PKCE (recommended)")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(settings.callback_port)
        self.contact = QLineEdit(settings.contact_email)
        self.contact.setPlaceholderText("goes in the User-Agent, as CCP asks")

        esi.addRow("Client ID", self.client_id)
        esi.addRow("Client secret", self.client_secret)
        esi.addRow("Callback port", self.port)
        esi.addRow("Contact email", self.contact)

        self.callback_hint = QLabel()
        self.callback_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.callback_hint.setWordWrap(True)
        esi.addRow("Callback URL", self.callback_hint)
        self.port.valueChanged.connect(self._update_hint)
        self._update_hint()
        root.addWidget(esi_box)

        # --- pricing
        price_box = QGroupBox("Pricing")
        price = QFormLayout(price_box)
        blurb = QLabel(
            "Everything is priced on two bases: the highest Jita 4-4 bid and the "
            "lowest Jita 4-4 ask. The groups below fall back to an average of "
            "public item-exchange contracts when Jita has no usable order book "
            "for them, which is most of the time for capitals."
        )
        blurb.setWordWrap(True)
        price.addRow(blurb)

        self.contract_first = QCheckBox(
            "Always prefer the contract average for these groups, even when Jita has orders"
        )
        self.contract_first.setChecked(settings.contract_price_beats_market)
        self.contract_first.setToolTip(
            "Off: use Jita whenever it has both a bid and an ask, and fall back to "
            "contracts otherwise. On: contracts win, which is closer to reality "
            "when the only Jita order is a token listing."
        )
        price.addRow(self.contract_first)
        self.groups = QPlainTextEdit("\n".join(settings.contract_priced_groups))
        self.groups.setPlaceholderText("One SDE group name per line, e.g. Freighter")
        self.groups.setMaximumHeight(150)
        price.addRow("Contract-priced groups", self.groups)

        self.regions = QLineEdit(
            ", ".join(str(r) for r in settings.contract_scan_regions)
        )
        self.regions.setToolTip("Region IDs scanned for public contracts, comma separated")
        price.addRow("Contract scan regions", self.regions)

        self.min_price = QDoubleSpinBox()
        self.min_price.setRange(0, 1e12)
        self.min_price.setDecimals(0)
        self.min_price.setGroupSeparatorShown(True)
        self.min_price.setValue(settings.contract_min_price)
        self.min_price.setToolTip("Ignore contracts cheaper than this when scanning")
        price.addRow("Min contract price (ISK)", self.min_price)

        self.min_volume = QDoubleSpinBox()
        self.min_volume.setRange(0, 1e9)
        self.min_volume.setDecimals(0)
        self.min_volume.setGroupSeparatorShown(True)
        self.min_volume.setValue(settings.contract_min_volume)
        self.min_volume.setToolTip(
            "Ignore contracts smaller than this. A packaged capital is 1,300,000 m3; "
            "the default of 500,000 sits above any packaged subcapital."
        )
        price.addRow("Min contract volume (m3)", self.min_volume)

        self.iqr = QDoubleSpinBox()
        self.iqr.setRange(0.0, 10.0)
        self.iqr.setSingleStep(0.25)
        self.iqr.setValue(settings.contract_outlier_iqr)
        self.iqr.setToolTip(
            "Contract prices further than this many IQRs outside the quartiles are "
            "dropped before averaging. 0 disables the filter."
        )
        price.addRow("Outlier rejection (IQR)", self.iqr)
        root.addWidget(price_box)

        # --- behaviour
        behave_box = QGroupBox("Behaviour")
        behave = QFormLayout(behave_box)
        self.snapshot = QCheckBox("Record a net worth snapshot after every sync")
        self.snapshot.setChecked(settings.snapshot_on_sync)
        behave.addRow(self.snapshot)
        self.abyssal_on_sync = QCheckBox("Fetch rolls for new abyssal items during sync")
        self.abyssal_on_sync.setChecked(settings.abyssal_stats_on_sync)
        self.abyssal_on_sync.setToolTip(
            "ESI serves abyssal rolls one item per request, so a sync that fetches "
            "them takes longer by about half a second per new abyssal module. "
            "Items already fetched are never asked for again."
        )
        behave.addRow(self.abyssal_on_sync)

        self.check_sde = QCheckBox("Check for new game data at startup")
        self.check_sde.setChecked(settings.check_sde_on_startup)
        self.check_sde.setToolTip(
            "Asks CCP which game data build is current, which is a single "
            "80-byte request, and offers to download it if yours is older. "
            "Nothing is downloaded without you agreeing to it."
        )
        behave.addRow(self.check_sde)

        self.debug_logging = QCheckBox("Write a debug log")
        self.debug_logging.setChecked(settings.debug_logging)
        self.debug_logging.setToolTip(
            "Records every ESI request and its status, each sync step, and any "
            "unhandled error, to:\n"
            f"{LOG_PATH}\n\n"
            "Off by default because those lines name your characters and what "
            "they hold. Turn it on when reporting a problem, then send that "
            "file. Takes effect immediately."
        )
        behave.addRow(self.debug_logging)
        root.addWidget(behave_box)

        root.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        root.addWidget(box)

    def _update_hint(self) -> None:
        url = f"http://localhost:{self.port.value()}{self.settings.callback_path}"
        self.callback_hint.setText(
            f"<b>{url}</b><br>Register this exact URL on your application at "
            "developers.eveonline.com."
        )

    def save(self) -> None:
        s = self.settings
        s.client_id = self.client_id.text().strip()
        s.client_secret = self.client_secret.text().strip()
        s.callback_port = self.port.value()
        s.contact_email = self.contact.text().strip()
        s.contract_priced_groups = [
            line.strip() for line in self.groups.toPlainText().splitlines() if line.strip()
        ]
        regions = []
        for part in self.regions.text().replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                regions.append(int(part))
        s.contract_scan_regions = regions
        s.contract_outlier_iqr = self.iqr.value()
        s.contract_min_price = self.min_price.value()
        s.contract_min_volume = self.min_volume.value()
        s.contract_price_beats_market = self.contract_first.isChecked()
        s.snapshot_on_sync = self.snapshot.isChecked()
        s.abyssal_stats_on_sync = self.abyssal_on_sync.isChecked()
        s.check_sde_on_startup = self.check_sde.isChecked()
        s.debug_logging = self.debug_logging.isChecked()
        # Applied here rather than at the next launch: somebody ticking
        # this is trying to capture something that is happening now.
        configure(s.debug_logging)
        s.save()
        self.accept()

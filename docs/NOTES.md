# ICE: Herkunft, Einrichtung und Testgrenzen

ICE ist eine separate AGPL-Anwendung mit eigenem Git-Verlauf. Die vertrauten
Templates, Styles und Anwendungsmodule wurden gezielt übernommen und für eine
leere, unabhängig installierbare Anwendung angepasst. Persönliche Daten,
Konfiguration, Schlüssel, Kurslisten und laufende Anwendungen sind keine Quelle
für die Einrichtung. Andere StudyCenter-Versionen bleiben unverändert.

## Komponenten

Die gemeinsame Einrichtung verwendet uv 0.8.22 (Astral, MIT/Apache-2.0), mit festen
SHA256-Werten für die offiziellen Windows-/Linux-Archive. Python 3.12 wird
vorhanden verwendet oder lokal verwaltet. Der Paketindex liefert die in
`pyproject.toml` festgelegten Bibliotheken; Plattform-Constraints stehen in den
beiden Requirements-Dateien. uv verwaltet Pakete und Cache.

Die optionalen portablen OCR-/Browserarchive und Lizenzhinweise sind unveränderte,
mit SHA256 und Größe gebundene Komponenten der vorhandenen öffentlichen beta.3.
Die Manifeste unter `packaging/` nennen vollständige Herkunft und Prüfsummen.
Es wird niemals die Anwendung oder ein Python-Wheel der anderen Public-Version
installiert. Quellangebote/Lizenzen der nativen Komponenten bleiben neben deren
Laufzeit erhalten. Sprachgewichte sind ein eigener, ausdrücklicher Download;
ein Einschalten oder Neustart ist keine Downloadfreigabe.

## Absichtliche Grenzen

- Kein privater Hosted-MCP-Tunnel, keine geteilten Remote-IDs oder Zugangsschlüssel.
- Linux hat keine Windows-Desktop-/Autostart-/Toast-Funktionen; die Anwendung und
  der Kalender funktionieren unabhängig vom automatisierten TU-Browser.
- Keine Umgehung von AppArmor oder Browser-Sandbox. Ein blockierter TU-Browser
  wird sichtbar gemeldet; vorhandene Inhalte bleiben nutzbar.
- Eigene OAuth-/TU-/Mail-Anmeldungen sind erforderlich. Tests mit synthetischen
  Daten beweisen keine erfolgreiche Anmeldung bei realen Anbietern.
- Die Windows-Setup-Datei wird gebaut, aber auf dem produktiven Rechner nicht
  installiert oder deinstalliert. Eine frische Windows-/Linux-Installation ist
  erst nach tatsächlich durchgeführten Gasttests als abgenommen zu bezeichnen.

## Prüfen

`python -m pytest` prüft die übernommene Funktionslogik und die ICE-Trennung mit
synthetischen Daten. `python scripts/check_public.py --staged` ist vor jedem
öffentlichen Commit erforderlich; Quellarchive werden zusätzlich entpackt geprüft.
Oberflächenprüfungen verwenden eigene synthetische Bestände und keinen Zugriff
auf persönliche Anwendungsdaten.

# StudyCenter Public ICE

Die vertraute StudyCenter-Oberfläche als eigenständige, lokale Anwendung.
ICE startet **leer**: keine vorgegebenen Kurse, Dokumente, Konten, Sitzungen,
persönlichen Einstellungen oder Zugangsdaten. AGPL-3.0-only.

## Starten

**Windows 10/11 x64:** [Setup herunterladen](https://github.com/Saltybukket/StudyCenter-Public-ICE/releases),
installieren und ICE öffnen. Alternativ die portable ZIP entpacken und `start.cmd`
ausführen. Die erste Einrichtung benötigt Internet und zeigt ihren Fortschritt.

**Linux x64 (Ubuntu 24.04 als Zielsystem):** TAR.GZ entpacken, im Ordner
`sh start.sh` ausführen. Die Oberfläche öffnet sich im normalen Systembrowser.
Der gesteuerte TU-Browser ist davon getrennt und benötigt eine funktionierende
Browser-Sandbox. Sicherheitsrichtlinien werden nicht verändert.

**Entwicklung:** Repository klonen, `start.cmd` oder `sh start.sh` ausführen.
Vorhandenes CPython 3.12 x64 wird benutzt; andernfalls lädt uv eine eigene
Laufzeit. Alle Pakete landen in einer eigenen virtuellen Umgebung. Ein erneuter
Start verwendet die Einrichtung und Downloads wieder. Nach einem Abbruch denselben
Starter erneut ausführen. `--stop` beendet ausschließlich die passende ICE-Instanz
und lädt nichts herunter. `--setup-only`, `--no-browser`, `--browser`, `--update`
und `--data-dir ORDNER` sind ebenfalls verfügbar.

## Was enthalten ist

- Übersicht, Timeline, Aufgaben und Post-its, Kalender mit Wochen-/Monatsansicht,
  Informationen/Änderungsverlauf, Archiv, Kurse/Quellen, Dokumente und TU-Mail.
- Dieselben Einstellungsbereiche: Benachrichtigungen, Sprache, Darstellung,
  Verbindungen, Google Kalender, Sicherungen und Betrieb. Die Formulare bedienen
  die tatsächliche Anwendung; Zugangsdaten werden selbst eingerichtet.
- Lokaler Assistent mit Vorschau, persönlichen Aktionen, Belegen und Rücknahme;
  Modellverbindung, lokale Lernhinweise, Suche und lokaler MCP-Zugang.
- OCR, eigener TU-Browser und Sprachbibliotheken werden unter Betrieb ausdrücklich
  eingerichtet; Sprachmodelle unter Sprache separat geladen. Keine Tesseract- oder
  anderen Systeminstaller. Originale und Dokumentfassungen bleiben erhalten.

Quellenabrufe erfolgen manuell. Modellzugänge, Google-Freigaben und Mailkonten sind
optional und beginnen unverbunden; ihre Benutzung kann eigene Anbieterbedingungen
oder Kosten haben. ICE wechselt nach OAuth-Fehlern nicht zu einer kostenpflichtigen API.
Es werden weder fremde Zugangsdaten noch vorhandene Browserprofile importiert.

## Daten und Unterschiede

ICE verwendet Port **18791** sowie eigene Profile und Zugangsspeicher. Der Starter
legt seine Daten unter `.local/data` im eigenen Ordner an; ein anderer **leerer**
Ordner ist über `--data-dir` wählbar. Sicherungen enthalten persönliche Inhalte und
sollten entsprechend aufbewahrt werden. Zugangsdaten sind davon getrennt.

Die bestehende Public-Version wird nicht aktualisiert oder ersetzt. Hosted MCP /
Tunnel ist hier nicht eingerichtet; lokale MCP-Verbindungen bleiben verfügbar.
Windows-Fenster, Tray, native Windows-Meldungen und Windows-Autostart sind unter
Linux nicht verfügbar. Linux nutzt den Systembrowser; Zugänge bleiben ohne
ausdrücklich eingerichteten sicheren Schlüsselbund nur für die Sitzung gespeichert.
Google benötigt einen eigenen OAuth-Desktop-Client und eine persönliche Freigabe.

Weitere Angaben, Testgrenzen und Komponentenherkunft: [Hinweise](docs/NOTES.md).

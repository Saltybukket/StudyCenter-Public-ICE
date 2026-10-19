# StudyCenter Public ICE

Die vertraute StudyCenter-Oberfläche für deine eigene Studienorganisation –
mit Aufgaben, Post-its, Kalender, Dokumenten und lokalem KI-Assistenten.
ICE startet leer: Du richtest deine Kurse und Verbindungen selbst ein.

## Herunterladen

**Aktuelle Version: ICE beta.3** · Windows und Linux, jeweils x64

| System | Download | Start |
| --- | --- | --- |
| **Windows 10/11** | [**Windows-Setup (.exe)**](https://github.com/Saltybukket/StudyCenter-Public-ICE/releases/download/v0.1.0-ice-beta.3/studycenter-public-ice-0.1.0b3-windows-x86_64-setup.exe) | Setup ausführen, anschließend ICE öffnen |
| Windows, portabel | [Portable ZIP](https://github.com/Saltybukket/StudyCenter-Public-ICE/releases/download/v0.1.0-ice-beta.3/studycenter-public-ice-0.1.0b3-windows-x86_64-portable.zip) | Entpacken und `start.cmd` ausführen |
| **Linux** | [**Linux-Paket (.tar.gz)**](https://github.com/Saltybukket/StudyCenter-Public-ICE/releases/download/v0.1.0-ice-beta.3/studycenter-public-ice-0.1.0b3-linux-x86_64.tar.gz) | Entpacken, im Ordner `sh start.sh` ausführen |

[Versionshinweise und Prüfsummen](https://github.com/Saltybukket/StudyCenter-Public-ICE/releases/tag/v0.1.0-ice-beta.3) · [Alle Versionen](https://github.com/Saltybukket/StudyCenter-Public-ICE/releases)

Beim ersten Start werden Internet und etwas Zeit für die Einrichtung benötigt.
Vorhandenes CPython 3.12 x64 wird verwendet; andernfalls lädt ICE eine eigene
Laufzeit. Pakete, Profile und Daten bleiben im eigenen ICE-Verzeichnis.
Nach einem Abbruch denselben Starter erneut ausführen. Weitere Starts verwenden
die Einrichtung und bereits geladene Komponenten wieder.

## Erste Schritte in der App

Beim ersten Öffnen eines leeren ICE-Profils begrüßt dich eine visuelle Tour.
Sie erklärt Kurse und Quellen, KI-Zugang, Benachrichtigungen, Darstellung und
optionale Extras. Die Tour öffnet die echten Einstellungsformulare; anschließend
geht es mit **Einrichtung fortsetzen** zurück. Du kannst pausieren und später
über **Einrichtung** in der Navigation weitergehen. Bestehende Studienbestände
werden nicht unterbrochen. Die Tour allein startet keine Downloads oder Anmeldungen.

## Was du nutzen kannst

- **Organisieren:** Übersicht, Timeline, Aufgaben, Post-its, Wochen-/Monatskalender und Archiv.
- **Studieren:** Kurse, Quellen, Dokumente, Suche, Änderungen und TU-Mail; Quellenabrufe erfolgen manuell.
- **Assistenz:** Modellverbindung, lokaler Assistent mit Vorschau, Belegen und Rücknahme sowie lokaler MCP-Zugang.
- **Einstellen:** Benachrichtigungen, Sprache, Darstellung, Verbindungen, Google Kalender, Sicherungen und Betrieb.

OCR, TU-Browser und Sprachbibliotheken richtest du bei Bedarf unter **Betrieb**
ein; Sprachmodelle lädst du separat unter **Sprache**. Konten und Zugänge beginnen
unverbunden. Google benötigt einen eigenen OAuth-Client. Anbieter können eigene
Bedingungen oder Kosten haben; ICE wechselt nicht automatisch zu einer bezahlten API.

## Starten aus dem Repository

```sh
git clone https://github.com/Saltybukket/StudyCenter-Public-ICE.git
cd StudyCenter-Public-ICE
```

Dann **Windows:** `start.cmd` · **Linux:** `sh start.sh`.

Nützliche Optionen: `--stop`, `--no-browser`, `--setup-only`, `--update` und
`--data-dir ORDNER` für einen anderen leeren Datenordner. `--stop` beendet nur
die passende ICE-Instanz und lädt nichts herunter.

## Daten und Plattformen

Daten liegen standardmäßig unter `.local/data`; ICE nutzt Port **18791** und
getrennte Zugangsspeicher. Vorhandene StudyCenter-Installationen bleiben unverändert.
Sicherungen können persönliche Inhalte enthalten und sollten sicher aufbewahrt werden.

Unter **Linux** öffnet sich die Oberfläche im normalen Systembrowser. Der separate
TU-Browser benötigt eine funktionierende Sandbox; Sicherheitsrichtlinien werden
nicht verändert. Windows-Fenster, Tray, native Windows-Meldungen und Windows-Autostart
sind dort nicht verfügbar. Ohne ausdrücklich eingerichteten sicheren Schlüsselbund
bleiben Linux-Zugänge nur für die Sitzung gespeichert. Zielsystem: Ubuntu 24.04 x64.
Hosted MCP / Tunnel ist nicht eingerichtet; lokales MCP ist verfügbar.

**Beta:** Prüfungen, verbleibende Testgrenzen und Komponentenherkunft stehen in den
[Hinweisen](docs/NOTES.md). Lizenz: [AGPL-3.0-only](LICENSE).

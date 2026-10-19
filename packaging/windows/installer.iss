#ifndef SourceRoot
#define SourceRoot "..\..\build\distribution\StudyCenter-Public-ICE"
#endif
#ifndef OutputDir
#define OutputDir "..\..\dist"
#endif
[Setup]
AppId={{63CE6879-5EA6-48DB-8ADC-69802C8EB9D7}
AppName=StudyCenter Public ICE
AppVersion=0.1.0b1
AppPublisher=StudyCenter Public ICE contributors
DefaultDirName={localappdata}\Programs\StudyCenter-Public-ICE
DefaultGroupName=StudyCenter Public ICE
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=studycenter-public-ice-0.1.0b1-windows-x86_64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
CloseApplications=no
RestartApplications=no
AppMutex=StudyCenterPublicICEInstallerOperation
UninstallDisplayIcon={uninstallexe}
[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung"; Flags: unchecked
[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\StudyCenter Public ICE"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; WorkingDir: "{app}"
Name: "{group}\StudyCenter Public ICE deinstallieren"; Filename: "{uninstallexe}"
Name: "{userdesktop}\StudyCenter Public ICE"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; WorkingDir: "{app}"; Tasks: desktopicon
[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; Description: "StudyCenter Public ICE starten und eigene Umgebung einrichten"; Flags: nowait postinstall skipifsilent

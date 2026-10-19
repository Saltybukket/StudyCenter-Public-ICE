Option Explicit
Dim shell, files, root, command, mode, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
command = Chr(34) & root & "\start.cmd" & Chr(34)
' Show first-time download progress; prepared launches stay quiet.
mode = 0
If Not files.FileExists(root & "\.local\installation.json") Then mode = 1
result = shell.Run(command & " --setup-only", mode, True)
If result <> 0 Then
  MsgBox "ICE konnte nicht eingerichtet werden. Bitte start.cmd im ICE-Ordner ausfuehren; dort wird der konkrete Fehler angezeigt.", 16, "StudyCenter Public ICE"
Else
  shell.Run command, 0, False
End If

Option Explicit
Dim shell, files, root, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
' The first setup stays visible to show progress and actionable errors.
' After setup, the native application owns its own window and tray.
command = Chr(34) & root & "\start.cmd" & Chr(34)
shell.Run command, 1, False

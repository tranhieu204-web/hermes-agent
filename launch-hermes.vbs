Option Explicit
Dim files, shell, runtime, launcher
runtime = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
launcher = "C:\SakaanAIAgentWorkspace\Hermes\main\launch-hermes.ps1"
Set files = CreateObject("Scripting.FileSystemObject")
If Not files.FileExists(runtime) Or Not files.FileExists(launcher) Then
  MsgBox "Canonical Hermes launcher is missing. Nothing was started.", 16, "Hermes"
  WScript.Quit 1
End If
Set shell = CreateObject("WScript.Shell")
shell.Run """" & runtime & """ -NoLogo -NoProfile -WindowStyle Hidden -File """ & launcher & """", 0, False

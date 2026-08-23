' Claude Account Manager - system tray launcher (autostart entry)
Option Explicit
Dim sh, fso, userProfile, pythonw, script, exec, out

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

userProfile = sh.ExpandEnvironmentStrings("%USERPROFILE%")
script = userProfile & "\Documents\GitHub\claude-account-manager\tray_manager.py"

' Resolve pythonw.exe dynamically via the "py" launcher instead of a
' hardcoded install path/version, so this works on any machine that has
' the standard python.org "py" launcher installed.
pythonw = ""
On Error Resume Next
Set exec = sh.Exec("py -3 -c ""import sys; print(sys.executable)""")
Do While exec.Status = 0
    WScript.Sleep 50
Loop
out = Trim(exec.StdOut.ReadAll())
On Error Goto 0
If out <> "" And fso.FileExists(out) Then
    pythonw = Replace(out, "python.exe", "pythonw.exe")
End If

If pythonw = "" Or Not fso.FileExists(pythonw) Then WScript.Quit 0
If Not fso.FileExists(script) Then WScript.Quit 0

sh.Run """" & pythonw & """ """ & script & """", 0, False

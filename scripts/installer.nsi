; KAM Sentinel — NSIS Installer Script
; Wraps dist\KAM_Sentinel_Windows.exe into a proper Windows installer.
;
; Usage (from project root):
;   makensis /DVER=1.5.5 scripts\installer.nsi
;
; Output: dist\KAM_Sentinel_Setup.exe

; Resolve all relative paths from the project root, not the scripts/ dir
!cd ".."

!include "MUI2.nsh"
!include "WordFunc.nsh"   ; provides VersionCompare

;----------------------------------------------------------------
; Build-time version (passed via /DVER= flag; fallback to 0.0.0)

!ifndef VER
  !define VER "0.0.0"
!endif

;----------------------------------------------------------------
; General

  Name              "KAM Sentinel"
  OutFile           "dist\KAM_Sentinel_Setup.exe"
  InstallDir        "$LOCALAPPDATA\KAM Sentinel"
  InstallDirRegKey  HKCU "Software\KAM Sentinel" "InstallDir"
  RequestExecutionLevel user
  Unicode           True

  ; Installer/uninstaller icon
  !define MUI_ICON   "assets\icon.ico"
  !define MUI_UNICON "assets\icon.ico"

  ; Warn if user clicks Abort during installation
  !define MUI_ABORTWARNING

  ; Welcome page text
  !define MUI_WELCOMEPAGE_TITLE "Welcome to KAM Sentinel ${VER} Setup"
  !define MUI_WELCOMEPAGE_TEXT \
    "KAM Sentinel is a real-time PC performance dashboard for gamers \
and power users — CPU, GPU, RAM, temps, FPS, and smart warnings.$\r$\n\
$\r$\nThis wizard will install KAM Sentinel on your computer."

  ; Finish page — offer to launch the app
  !define MUI_FINISHPAGE_RUN          "$INSTDIR\KAM_Sentinel_Windows.exe"
  !define MUI_FINISHPAGE_RUN_TEXT     "Launch KAM Sentinel now"
  !define MUI_FINISHPAGE_SHOWREADME   ""
  !define MUI_FINISHPAGE_LINK         "Visit project page"
  !define MUI_FINISHPAGE_LINK_LOCATION "https://kypin00-web.github.io/KAM-Sentinel"

;----------------------------------------------------------------
; Installer pages

  !insertmacro MUI_PAGE_WELCOME
  !insertmacro MUI_PAGE_DIRECTORY
  !insertmacro MUI_PAGE_INSTFILES
  !insertmacro MUI_PAGE_FINISH

;----------------------------------------------------------------
; Uninstaller pages

  !insertmacro MUI_UNPAGE_CONFIRM
  !insertmacro MUI_UNPAGE_INSTFILES

;----------------------------------------------------------------
; Language

  !insertmacro MUI_LANGUAGE "English"

;----------------------------------------------------------------
; Installer section

Section "KAM Sentinel" SecMain

  SetOutPath "$INSTDIR"

  ; ── Version guard — never let an old installer overwrite a newer install ────
  ; Read the version we wrote to the registry on the previous install.
  ; If installed version > bundled version, abort with a friendly message.
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" "DisplayVersion"
  ${If} $0 != ""
    ${VersionCompare} $0 "${VER}" $1
    ${If} $1 == 1   ; $1==1 means installed ($0) is newer than bundled (${VER})
      MessageBox MB_OK|MB_ICONINFORMATION \
        "KAM Sentinel $0 is already installed.$\r$\n\
This installer contains an older version (${VER}) and will not downgrade your installation.$\r$\n$\r$\n\
Your current version is untouched."
      Abort
    ${EndIf}
  ${EndIf}

  ; ── Kill running instances before overwriting the exe ──────────────────────
  ; The installer can't replace a file that's currently held open by Windows.
  ; These commands are best-effort — exit code is ignored via ExecToLog.
  nsExec::ExecToLog 'taskkill /F /IM "KAM Sentinel.exe" /T'
  nsExec::ExecToLog 'taskkill /F /IM "KAM_Sentinel_Windows.exe" /T'
  nsExec::ExecToLog 'taskkill /F /IM "kam_launcher.exe" /T'
  Sleep 500

  ; Copy the main executable
  File "dist\KAM_Sentinel_Windows.exe"

  ; ── Unblock Mark of the Web ────────────────────────────────
  ; Files downloaded from the internet carry a Zone.Identifier ADS that
  ; causes Windows to block execution. Unblock immediately after extraction
  ; so the app launches without "cannot access the specified device" errors.
  nsExec::ExecToLog 'powershell -NoProfile -NonInteractive -Command "Get-ChildItem -LiteralPath \"$INSTDIR\" -Recurse | Unblock-File"'

  ; ── Shortcuts ──────────────────────────────────────────────
  CreateDirectory "$SMPROGRAMS\KAM Sentinel"
  CreateShortcut \
    "$SMPROGRAMS\KAM Sentinel\KAM Sentinel.lnk" \
    "$INSTDIR\KAM_Sentinel_Windows.exe" \
    "" "$INSTDIR\KAM_Sentinel_Windows.exe" 0

  CreateShortcut \
    "$DESKTOP\KAM Sentinel.lnk" \
    "$INSTDIR\KAM_Sentinel_Windows.exe" \
    "" "$INSTDIR\KAM_Sentinel_Windows.exe" 0

  ; ── Add/Remove Programs registry (HKCU — no admin required) ───────────────
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "DisplayName"     "KAM Sentinel"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "DisplayIcon"     "$INSTDIR\KAM_Sentinel_Windows.exe"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "Publisher"       "KAM Sentinel"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "DisplayVersion"  "${VER}"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "URLInfoAbout"    "https://kypin00-web.github.io/KAM-Sentinel"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "URLUpdateInfo"   "https://github.com/kypin00-web/KAM-Sentinel/releases"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "NoRepair" 1

  ; Store install location for future reference
  WriteRegStr HKCU "Software\KAM Sentinel" "InstallDir" "$INSTDIR"

  ; Write the uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; ── Clean up PyInstaller _MEI temp folders ─────────────────────────────────
  ; PyInstaller extracts to %LOCALAPPDATA%\Temp\_MEI<random> each run and
  ; cannot self-delete the folder while the exe is running. Purge leftovers now
  ; that the old process is gone.
  nsExec::ExecToLog 'cmd /C for /d %i in ("$LOCALAPPDATA\Temp\_MEI*") do rd /s /q "%i"'

SectionEnd

;----------------------------------------------------------------
; Uninstaller section

Section "Uninstall"

  ; Remove files
  Delete "$INSTDIR\KAM_Sentinel_Windows.exe"
  Delete "$INSTDIR\Uninstall.exe"

  ; Remove shortcuts
  Delete "$SMPROGRAMS\KAM Sentinel\KAM Sentinel.lnk"
  RMDir  "$SMPROGRAMS\KAM Sentinel"
  Delete "$DESKTOP\KAM Sentinel.lnk"

  ; Remove registry
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel"
  DeleteRegKey HKCU "Software\KAM Sentinel"

  ; Remove install directory (only if empty after above deletes)
  RMDir "$INSTDIR"

SectionEnd

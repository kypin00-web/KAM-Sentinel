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

;----------------------------------------------------------------
; Build-time version (passed via /DVER= flag; fallback to 0.0.0)

!ifndef VER
  !define VER "0.0.0"
!endif

;----------------------------------------------------------------
; General

  Name              "KAM Sentinel"
  OutFile           "dist\KAM_Sentinel_Setup.exe"
  InstallDir        "$PROGRAMFILES64\KAM Sentinel"
  InstallDirRegKey  HKLM "Software\KAM Sentinel" "InstallDir"
  RequestExecutionLevel admin
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

  ; Copy the main executable
  File "dist\KAM_Sentinel_Windows.exe"

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

  ; ── Add/Remove Programs registry ───────────────────────────
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "DisplayName"     "KAM Sentinel"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "DisplayIcon"     "$INSTDIR\KAM_Sentinel_Windows.exe"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "Publisher"       "KAM Sentinel"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "DisplayVersion"  "${VER}"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "URLInfoAbout"    "https://kypin00-web.github.io/KAM-Sentinel"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "URLUpdateInfo"   "https://github.com/kypin00-web/KAM-Sentinel/releases"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel" \
                "NoRepair" 1

  ; Store install location for future reference
  WriteRegStr HKLM "Software\KAM Sentinel" "InstallDir" "$INSTDIR"

  ; Write the uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

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
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KAM Sentinel"
  DeleteRegKey HKLM "Software\KAM Sentinel"

  ; Remove install directory (only if empty after above deletes)
  RMDir "$INSTDIR"

SectionEnd

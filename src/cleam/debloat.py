"""Windows 10/11 debloat: privacy, ads, AI features, taskbar clutter, preinstalled apps.

Every change is recorded before it is made and can be undone exactly:

- A registry value's previous state (absent, or its type and data) and
  whether its key existed are written to a journal *before* the new value is
  set. Undo puts back what was there -- not "the Windows default", which is
  what the .reg undo files of other debloaters restore, and which is wrong on
  any machine that had its own setting.
- Services are registry values too (HKLM\\SYSTEM\\...\\Services\\<name>\\Start),
  so they are journalled the same way. Scheduled tasks record whether they
  were enabled. Apps are removed for the current user only, which leaves
  Windows' staged copy in place, so undo re-registers them from it.

What is in the catalogue, and what is not, came from reading Win11Debloat
(MIT, https://github.com/Raphire/Win11Debloat -- its app IDs and risk ratings
are used here), WinUtil, and Microsoft's policy documentation. The last one
decided a lot: several policies other tools apply as "essential" are
documented as Enterprise/Education only (DisableWindowsConsumerFeatures,
DisableSoftLanding, AllowTelemetry=0 as "off"), so on Home/Pro they do
nothing. Cleam uses the per-user settings that work on every edition and
marks edition-limited tweaks as unavailable rather than pretending.

Never offered: removing the Microsoft Store, Edge, Windows Terminal or the
Xbox identity/TCUI frameworks (other apps depend on them), or disabling
Windows Update, Defender or SmartScreen (a cleaner must not lower security).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .system import NO_WINDOW, OS, is_admin

HKCU, HKLM = "HKCU", "HKLM"
DWORD, SZ = 4, 1  # winreg.REG_DWORD, winreg.REG_SZ
SAFE, MODERATE = "safe", "moderate"


@dataclass(frozen=True)
class Reg:
    hive: str
    key: str
    name: str
    value: int | str
    kind: int = DWORD
    # Touch this value only when its key already exists: a service's Start
    # value for a service this Windows does not have, an app's settings key.
    if_key_exists: bool = False
    # What Windows has when nobody changed it, for "set back to default" on a
    # change Cleam did not make (so the journal has no before-value): None
    # means the value is simply absent, which is true of every policy.
    original: int | str | None = None


def service(name: str, start: int, original: int) -> Reg:
    """A service's startup type: 2 automatic, 3 manual, 4 disabled."""
    return Reg(HKLM, rf"SYSTEM\CurrentControlSet\Services\{name}", "Start", start, if_key_exists=True,
               original=original)


@dataclass(frozen=True)
class Tweak:
    id: str
    group: str
    title: str
    about: str  # what it does and what you lose, in plain words
    reg: tuple[Reg, ...] = ()
    tasks: tuple[str, ...] = ()  # scheduled tasks to disable, full path
    default: bool = False  # ticked in the recommended set
    risk: str = SAFE
    windows: str = "10,11"
    min_build: int = 0
    editions: tuple[str, ...] = ()  # EditionID prefixes it works on; () = every edition
    requires_file: str = ""  # only offered when this file exists (%VARS% expanded)
    restart: str = ""  # "explorer" | "sign out" | "restart" -- when it takes effect
    # A change made by a Windows command rather than a registry value
    # (reserved storage, hibernation): the name of an entry in ACTIONS.
    action: str = ""


@dataclass(frozen=True)
class App:
    id: str  # Appx package Name
    name: str
    about: str
    default: bool = False
    risk: str = SAFE


GROUPS = ("Power & storage", "Privacy", "Ads & suggestions", "Search & Start", "AI & Copilot",
          "Taskbar & Explorer", "Gaming & comfort", "Services", "Apps")

CDM = r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
ADV = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
POL = r"SOFTWARE\Policies\Microsoft\Windows"


def cdm(*names: str) -> tuple[Reg, ...]:
    return tuple(Reg(HKCU, CDM, n, 0) for n in names)


TWEAKS: tuple[Tweak, ...] = (
    # ------------------------------------------------------------ Power & storage
    Tweak("reserved-storage", "Power & storage", "Give back Reserved Storage (usually about 7 GB)",
          "Windows keeps several GB of C: aside so updates always have room. Turning it off gives that space "
          "back; updates still install, but need that much free space when they run.",
          action="reserved-storage", risk=MODERATE),
    Tweak("hibernation", "Power & storage", "Turn off hibernation (deletes hiberfil.sys)",
          "Frees hiberfil.sys, usually 40-75% of your RAM in size. The PC can no longer hibernate, and Fast "
          "Startup stops too (it is built on hibernation). Sleep still works.",
          action="hibernation", risk=MODERATE),
    Tweak("fast-startup", "Power & storage", "Turn off Fast Startup",
          "Shut down really shuts down: drivers and updates start clean next boot, and dual-boot disks are not "
          "left locked. Booting takes a few seconds longer.",
          (Reg(HKLM, r"SYSTEM\CurrentControlSet\Control\Session Manager\Power", "HiberbootEnabled", 0, original=1),),
          risk=MODERATE, restart="restart"),
    Tweak("delivery-optimization", "Power & storage", "Don't share updates with other PCs",
          "Windows stops uploading update pieces to other PCs on the internet, which uses your bandwidth. "
          "Updates still download normally from Microsoft.",
          (Reg(HKLM, rf"{POL}\DeliveryOptimization", "DODownloadMode", 0),), default=True),

    # ------------------------------------------------------------ Privacy
    Tweak("telemetry", "Privacy", "Send the least diagnostic data",
          "Sets diagnostic data to the lowest level this edition allows. Home and Pro cannot turn it off "
          "completely (Microsoft allows 'off' on Enterprise/Education only); they send 'Required' data.",
          (Reg(HKLM, rf"{POL}\DataCollection", "AllowTelemetry", 0),), default=True, restart="restart"),
    Tweak("telemetry-service", "Privacy", "Stop the telemetry service",
          "Disables 'Connected User Experiences and Telemetry' (DiagTrack), the service that uploads "
          "diagnostic data. Nothing else depends on it.",
          (service("DiagTrack", 4, original=2),), default=True, restart="restart"),
    Tweak("telemetry-tasks", "Privacy", "Stop data-collection scheduled tasks",
          "Disables the Customer Experience Improvement Program, disk diagnostic and feedback upload tasks.",
          tasks=(r"\Microsoft\Windows\Customer Experience Improvement Program\Consolidator",
                 r"\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip",
                 r"\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector",
                 r"\Microsoft\Windows\Feedback\Siuf\DmClient",
                 r"\Microsoft\Windows\Feedback\Siuf\DmClientOnScenarioDownload"),
          default=True),
    Tweak("compatibility-tasks", "Privacy", "Stop the compatibility appraiser",
          "Disables the tasks that inventory your apps and send it for upgrade checks. Feature upgrades may "
          "be offered later, because Windows knows less about what you run.",
          tasks=(r"\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser",
                 r"\Microsoft\Windows\Application Experience\ProgramDataUpdater"),
          risk=MODERATE),
    Tweak("advertising-id", "Privacy", "Turn off the advertising ID",
          "Apps can no longer use a per-user ID to show you personalised ads.",
          (Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled", 0),), default=True),
    Tweak("advertising-id-policy", "Privacy", "Turn off the advertising ID for every account",
          "The policy form: no account on this PC gets an advertising ID. Microsoft documents it for Pro and up.",
          (Reg(HKLM, rf"{POL}\AdvertisingInfo", "DisabledByGroupPolicy", 1),), default=True),
    Tweak("telemetry-extras", "Privacy", "Send no diagnostic logs or crash dumps",
          "Stops Windows attaching diagnostic logs and memory dumps to diagnostic data, and stops it downloading "
          "settings that change what it collects. Documented for Pro and up on Windows 11.",
          (Reg(HKLM, rf"{POL}\DataCollection", "LimitDiagnosticLogCollection", 1),
           Reg(HKLM, rf"{POL}\DataCollection", "LimitDumpCollection", 1),
           Reg(HKLM, rf"{POL}\DataCollection", "DisableOneSettingsDownloads", 1)), default=True, windows="11"),
    Tweak("tailored-experiences", "Privacy", "No tips based on your diagnostic data",
          "Microsoft stops using diagnostic data to personalise tips, ads and recommendations.",
          (Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Privacy",
               "TailoredExperiencesWithDiagnosticDataEnabled", 0),), default=True),
    Tweak("activity-history", "Privacy", "Turn off activity history",
          "Windows stops recording and uploading which apps and files you use. Clipboard history is kept.",
          (Reg(HKLM, rf"{POL}\System", "PublishUserActivities", 0),
           Reg(HKLM, rf"{POL}\System", "UploadUserActivities", 0)), default=True),
    Tweak("feedback-prompts", "Privacy", "No feedback requests",
          "Windows stops asking you to rate it and hides feedback notifications.",
          (Reg(HKCU, r"Software\Microsoft\Siuf\Rules", "NumberOfSIUFInPeriod", 0),
           Reg(HKLM, rf"{POL}\DataCollection", "DoNotShowFeedbackNotifications", 1)), default=True),
    Tweak("inking-typing", "Privacy", "Keep typing and handwriting to yourself",
          "Stops sending typing and inking samples to improve recognition, and stops harvesting contacts "
          "for the input dictionary.",
          (Reg(HKCU, r"Software\Microsoft\InputPersonalization", "RestrictImplicitInkCollection", 1),
           Reg(HKCU, r"Software\Microsoft\InputPersonalization", "RestrictImplicitTextCollection", 1),
           Reg(HKCU, r"Software\Microsoft\InputPersonalization\TrainedDataStore", "HarvestContacts", 0),
           Reg(HKCU, r"Software\Microsoft\Input\TIPC", "Enabled", 0)), default=True),
    Tweak("app-launch-tracking", "Privacy", "Don't track app launches",
          "Start stops ranking apps by how often you open them; its 'most used' list stops updating.",
          (Reg(HKCU, ADV, "Start_TrackProgs", 0),), default=True, restart="explorer"),
    Tweak("search-history", "Privacy", "Don't keep search history on this PC",
          "Windows Search stops remembering what you searched for.",
          (Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\SearchSettings", "IsDeviceSearchHistoryEnabled", 0),),
          default=True),
    Tweak("online-speech", "Privacy", "Turn off online speech recognition",
          "Voice typing and speech features stop sending audio to Microsoft. Dictation quality drops or stops.",
          (Reg(HKCU, r"Software\Microsoft\Speech_OneCore\Settings\OnlineSpeechPrivacy", "HasAccepted", 0),),
          risk=MODERATE),
    Tweak("location", "Privacy", "Turn off location for everything",
          "No app can read your location. Maps, weather, Find my device and automatic time zone stop working.",
          (Reg(HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location",
               "Value", "Deny", SZ, original="Allow"),), risk=MODERATE),
    Tweak("error-reporting", "Privacy", "Don't send crash reports",
          "Crashes are no longer reported to Microsoft, so fixes for them come from other people's reports.",
          (Reg(HKLM, r"SOFTWARE\Microsoft\Windows\Windows Error Reporting", "Disabled", 1),), risk=MODERATE),

    # ------------------------------------------------------------ Ads & suggestions
    Tweak("silent-app-installs", "Ads & suggestions", "Stop promoted apps installing themselves",
          "Windows stops installing sponsored apps (games, TikTok, Spotify...) without asking.",
          cdm("SilentInstalledAppsEnabled", "PreInstalledAppsEnabled", "OemPreInstalledAppsEnabled"),
          default=True),
    Tweak("start-suggestions", "Ads & suggestions", "No suggested apps in Start",
          "Removes 'suggestions' (ads for Store apps) from the Start menu.",
          cdm("SubscribedContent-338388Enabled", "SystemPaneSuggestionsEnabled")
          + (Reg(HKCU, ADV, "Start_IrisRecommendations", 0),), default=True, restart="explorer"),
    Tweak("tips", "Ads & suggestions", "No tips and tricks",
          "Windows stops showing 'tips, tricks and suggestions' as you use it.",
          cdm("SubscribedContent-338389Enabled", "SoftLandingEnabled"), default=True),
    Tweak("settings-suggestions", "Ads & suggestions", "No suggested content in Settings",
          "Removes promotions from the Settings app.",
          cdm("SubscribedContent-338393Enabled", "SubscribedContent-353694Enabled",
              "SubscribedContent-353696Enabled", "SubscribedContent-353698Enabled"), default=True),
    Tweak("welcome-experience", "Ads & suggestions", "No 'finish setting up' screens",
          "Stops the full-screen welcome and 'finish setting up your device' prompts after updates.",
          cdm("SubscribedContent-310093Enabled")
          + (Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement",
                 "ScoobeSystemSettingEnabled", 0),), default=True),
    Tweak("lockscreen-tips", "Ads & suggestions", "No tips on the lock screen",
          "Removes 'fun facts and tips' from the lock screen. Your picture stays.",
          cdm("SubscribedContent-338387Enabled", "RotatingLockScreenOverlayEnabled"), default=True),
    Tweak("explorer-ads", "Ads & suggestions", "No OneDrive/Office ads in File Explorer",
          "Hides sync-provider notifications (OneDrive and Microsoft 365 promotions) in File Explorer.",
          (Reg(HKCU, ADV, "ShowSyncProviderNotifications", 0),), default=True, restart="explorer"),
    Tweak("promo-notifications", "Ads & suggestions", "No promotional notifications",
          "Turns off 'suggested' notifications, account nags and backup reminders.",
          (Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\Windows.SystemToast.Suggested",
               "Enabled", 0),
           Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\Windows.SystemToast.BackupReminder",
               "Enabled", 0),
           Reg(HKCU, ADV, "Start_AccountNotifications", 0)), default=True),
    Tweak("device-companion-apps", "Ads & suggestions", "No vendor apps when you plug in a device",
          "Windows stops downloading companion apps (mouse, keyboard, printer software) for new devices. "
          "Drivers still install.",
          (Reg(HKLM, rf"{POL}\Device Metadata", "PreventDeviceMetadataFromNetwork", 1),), default=True),
    Tweak("consumer-features", "Ads & suggestions", "Turn off Microsoft consumer experiences (policy)",
          "The policy form of the tweaks above. Microsoft documents it for Enterprise and Education only; "
          "Home and Pro ignore it.",
          (Reg(HKLM, rf"{POL}\CloudContent", "DisableWindowsConsumerFeatures", 1),), default=True,
          editions=("Enterprise", "Education", "IoTEnterprise")),
    Tweak("settings-365-ads", "Ads & suggestions", "No Microsoft 365 ads in Settings",
          "Removes Microsoft 365 and account promotions from the Settings home page.",
          (Reg(HKLM, rf"{POL}\CloudContent", "DisableConsumerAccountStateContent", 1),), default=True,
          windows="11"),
    Tweak("edge-ads", "Ads & suggestions", "Edge: no ads, shopping or news feed",
          "Turns off Edge's sponsored new-tab content, shopping assistant, recommendations and first-run prompts.",
          (Reg(HKLM, r"SOFTWARE\Policies\Microsoft\Edge", "NewTabPageContentEnabled", 0),
           Reg(HKLM, r"SOFTWARE\Policies\Microsoft\Edge", "EdgeShoppingAssistantEnabled", 0),
           Reg(HKLM, r"SOFTWARE\Policies\Microsoft\Edge", "ShowRecommendationsEnabled", 0),
           Reg(HKLM, r"SOFTWARE\Policies\Microsoft\Edge", "SpotlightExperiencesAndRecommendationsEnabled", 0),
           Reg(HKLM, r"SOFTWARE\Policies\Microsoft\Edge", "PersonalizationReportingEnabled", 0)),
          default=True, requires_file=r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),

    # ------------------------------------------------------------ Search & Start
    Tweak("web-search", "Search & Start", "Search only this PC, not Bing",
          "Start and taskbar search stop sending what you type to Bing and show only local results.",
          (Reg(HKCU, r"Software\Policies\Microsoft\Windows\Explorer", "DisableSearchBoxSuggestions", 1),
           Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Search", "BingSearchEnabled", 0)),
          default=True, restart="explorer"),
    Tweak("search-highlights", "Search & Start", "No search highlights",
          "Removes the daily illustrations and trending content from the search box.",
          (Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\SearchSettings", "IsDynamicSearchBoxEnabled", 0),),
          default=True, restart="explorer"),
    Tweak("cortana", "Search & Start", "Turn off Cortana",
          "Disables Cortana in search. Microsoft has discontinued it anyway.",
          (Reg(HKLM, rf"{POL}\Windows Search", "AllowCortana", 0),), default=True, windows="10"),
    Tweak("phone-link-start", "Search & Start", "Hide the phone panel in Start",
          "Removes the Phone Link 'mobile device' panel from the Start menu.",
          (Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Start\Companions\Microsoft.YourPhone_8wekyb3d8bbwe",
               "IsEnabled", 0),), windows="11"),

    # ------------------------------------------------------------ AI & Copilot
    Tweak("copilot-button", "AI & Copilot", "Hide the Copilot button",
          "Removes Copilot from the taskbar. To remove the Copilot app itself, tick it under Apps.",
          (Reg(HKCU, ADV, "ShowCopilotButton", 0),), default=True, windows="11", restart="explorer"),
    Tweak("copilot-policy", "AI & Copilot", "Turn off Windows Copilot (older builds)",
          "The original Copilot policy. Microsoft is deprecating it and it does not control the newer "
          "Copilot app; kept for Windows 11 builds that still have the built-in Copilot.",
          (Reg(HKCU, r"Software\Policies\Microsoft\Windows\WindowsCopilot", "TurnOffWindowsCopilot", 1),),
          default=True, windows="11"),
    Tweak("recall", "AI & Copilot", "Turn off and remove Recall",
          "Stops Recall saving screenshots of your activity and removes the Recall component at the next "
          "restart, deleting its snapshots.",
          (Reg(HKLM, rf"{POL}\WindowsAI", "AllowRecallEnablement", 0),
           Reg(HKLM, rf"{POL}\WindowsAI", "DisableAIDataAnalysis", 1),
           Reg(HKCU, r"Software\Policies\Microsoft\Windows\WindowsAI", "DisableAIDataAnalysis", 1)),
          default=True, windows="11", min_build=26100, restart="restart"),
    Tweak("click-to-do", "AI & Copilot", "Turn off Click to Do",
          "Removes the AI actions offered on whatever is on screen.",
          (Reg(HKLM, rf"{POL}\WindowsAI", "DisableClickToDo", 1),
           Reg(HKCU, r"Software\Policies\Microsoft\Windows\WindowsAI", "DisableClickToDo", 1)),
          default=True, windows="11", min_build=26100),
    Tweak("ai-service", "AI & Copilot", "Don't start the AI service at boot",
          "Sets the Windows AI Fabric service to start only when something asks for it.",
          (service("WSAIFabricSvc", 3, original=2),), default=True, windows="11", restart="restart"),
    Tweak("ai-actions", "AI & Copilot", "Hide 'AI actions' in File Explorer",
          "Removes the AI actions submenu (image edits, summaries) from File Explorer's right-click menu.",
          (Reg(HKLM, rf"{POL}\Explorer", "HideAIActionsMenu", 1),), default=True, windows="11", min_build=26100,
          restart="explorer"),
    Tweak("app-ai", "AI & Copilot", "No AI features in Notepad and Paint",
          "Turns off Copilot rewrite in Notepad and Cocreator, generative fill and erase in Paint.",
          (Reg(HKLM, r"SOFTWARE\Policies\WindowsNotepad", "DisableAIFeatures", 1),
           Reg(HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Paint", "DisableCocreator", 1),
           Reg(HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Paint", "DisableGenerativeFill", 1),
           Reg(HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Paint", "DisableGenerativeErase", 1),
           Reg(HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Paint", "DisableImageCreator", 1)),
          windows="11"),

    # ------------------------------------------------------------ Taskbar & Explorer
    Tweak("file-extensions", "Taskbar & Explorer", "Show file extensions",
          "Shows .exe, .pdf, .docx... in File Explorer, so 'invoice.pdf.exe' can no longer pass for a PDF.",
          (Reg(HKCU, ADV, "HideFileExt", 0),), default=True, restart="explorer"),
    Tweak("widgets", "Taskbar & Explorer", "Turn off Widgets / News and interests",
          "Removes the news and weather feed from the taskbar.",
          (Reg(HKLM, r"SOFTWARE\Policies\Microsoft\Dsh", "AllowNewsAndInterests", 0),
           Reg(HKLM, rf"{POL}\Windows Feeds", "EnableFeeds", 0)), default=True, restart="explorer"),
    Tweak("meet-now", "Taskbar & Explorer", "Hide Meet Now / Chat",
          "Removes the Meet Now (Windows 10) or Teams Chat (Windows 11) button from the taskbar.",
          (Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "HideSCAMeetNow", 1),
           Reg(HKCU, ADV, "TaskbarMn", 0)), default=True, restart="explorer"),
    Tweak("people-bar", "Taskbar & Explorer", "Hide People on the taskbar",
          "Removes the People button (Windows 10).",
          (Reg(HKCU, rf"{ADV}\People", "PeopleBand", 0),), default=True, windows="10", restart="explorer"),
    Tweak("task-view-button", "Taskbar & Explorer", "Hide the Task View button",
          "Removes the Task View button. Win+Tab still opens it.",
          (Reg(HKCU, ADV, "ShowTaskViewButton", 0),), restart="explorer"),
    Tweak("classic-context-menu", "Taskbar & Explorer", "Classic right-click menu",
          "Brings back the full Windows 10 right-click menu instead of 'Show more options'.",
          (Reg(HKCU, r"Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32", "", "", SZ),),
          windows="11", restart="explorer"),
    Tweak("explorer-this-pc", "Taskbar & Explorer", "Open File Explorer on This PC",
          "File Explorer opens on your drives instead of Home / Quick access with recent files.",
          (Reg(HKCU, ADV, "LaunchTo", 1),)),
    Tweak("explorer-home-gallery", "Taskbar & Explorer", "Hide Home and Gallery in File Explorer",
          "Removes Home (recent files, recommendations) and Gallery from the left pane of File Explorer.",
          # Home needs its display name too: creating the per-user key hides the
          # machine-wide one, and without it the pane shows a blank entry.
          (Reg(HKCU, r"Software\Classes\CLSID\{f874310e-b6b7-47dc-bc84-b9e6b38f5903}", "", "CLSID_MSGraphHomeFolder", SZ),
           Reg(HKCU, r"Software\Classes\CLSID\{f874310e-b6b7-47dc-bc84-b9e6b38f5903}", "System.IsPinnedToNameSpaceTree", 0),
           Reg(HKCU, r"Software\Classes\CLSID\{e88865ea-0e1c-4e20-9aa6-edcd0212c87c}", "System.IsPinnedToNameSpaceTree", 0)),
          windows="11", restart="explorer"),
    Tweak("end-task", "Taskbar & Explorer", "'End task' on taskbar right-click",
          "Adds End task to a taskbar button's menu, to close a frozen app without Task Manager.",
          (Reg(HKCU, rf"{ADV}\TaskbarDeveloperSettings", "TaskbarEndTask", 1),), windows="11", min_build=22631),

    # ------------------------------------------------------------ Gaming
    Tweak("sticky-keys", "Gaming & comfort", "No Sticky Keys pop-up",
          "Pressing Shift five times (easy to do in a game) no longer opens the Sticky Keys prompt. Only that "
          "shortcut changes: if you use Sticky Keys, it stays on.",
          action="sticky-keys-hotkey"),
    Tweak("game-mode", "Gaming & comfort", "Game Mode on",
          "Windows gives the game priority and holds back Windows Update installs and driver prompts while you "
          "play. It is on by default; this turns it back on if something switched it off.",
          (Reg(HKCU, r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1),
           Reg(HKCU, r"Software\Microsoft\GameBar", "AllowAutoGameMode", 1)), default=True),
    Tweak("windowed-games", "Gaming & comfort", "Optimizations for windowed games",
          "DirectX 10/11 games in windowed or borderless mode use the flip model: lower input latency, and "
          "variable refresh rate and Auto HDR start working (Microsoft). Other graphics settings are kept.",
          action="windowed-games", default=True, windows="11", min_build=22621),
    Tweak("hags", "Gaming & comfort", "Hardware-accelerated GPU scheduling",
          "The GPU schedules its own work (Microsoft DirectX). Needs a GPU and driver that support it (NVIDIA "
          "10-series, AMD RX 5000 or newer); DLSS Frame Generation requires it. Results vary by game.",
          (Reg(HKLM, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", 2),),
          risk=MODERATE, restart="restart"),
    Tweak("mouse-acceleration", "Gaming & comfort", "Turn off mouse acceleration",
          "Turns off 'Enhance pointer precision': the cursor moves the same distance for the same hand movement, "
          "whatever the speed. Most aiming games expect this.",
          (Reg(HKCU, r"Control Panel\Mouse", "MouseSpeed", "0", SZ, original="1"),
           Reg(HKCU, r"Control Panel\Mouse", "MouseThreshold1", "0", SZ, original="6"),
           Reg(HKCU, r"Control Panel\Mouse", "MouseThreshold2", "0", SZ, original="10")), restart="sign out"),
    Tweak("power-plan", "Gaming & comfort", "High performance power plan",
          "The processor stops parking cores and dropping clocks between frames. More power and heat; on a laptop "
          "the battery drains faster. Any non-Balanced plan you already use (Ultimate, vendor plans) counts as on.",
          action="power-plan", risk=MODERATE),
    Tweak("virtual-machine-platform", "Gaming & comfort", "Turn off Virtual Machine Platform",
          "Microsoft's gaming guide lists it as costing performance in some games. WSL 2, Windows Subsystem for "
          "Android and Docker Desktop need it -- leave it on if you use them.",
          action="virtual-machine-platform", risk=MODERATE, restart="restart"),
    Tweak("game-dvr", "Gaming & comfort", "Turn off background game recording",
          "Stops Game DVR recording gameplay in the background, which costs frame rate. The Game Bar overlay "
          "and its FPS counter stay.",
          (Reg(HKCU, r"System\GameConfigStore", "GameDVR_Enabled", 0),
           Reg(HKCU, r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", 0),
           Reg(HKLM, rf"{POL}\GameDVR", "AllowGameDVR", 0)), risk=MODERATE),

    # ------------------------------------------------------------ Services
    Tweak("retail-demo", "Services", "Disable the retail demo service",
          "The service that runs store display mode. Nothing uses it at home.",
          (service("RetailDemo", 4, original=3),), default=True),
    Tweak("maps-broker", "Services", "Start the maps service only when needed",
          "Downloaded-maps manager set to manual: it starts when the Maps app needs it, not at every boot.",
          (service("MapsBroker", 3, original=2),), default=True),
)

# From Win11Debloat's Apps.json (MIT): the ones it rates "safe" are ticked by
# default here -- except apps holding data that exists only on this PC, since
# Remove-AppxPackage deletes an app's data (-PreserveApplicationData works for
# apps under development only) and undo brings back the app, not the notes --
# its "optional" ones are offered unticked, and its "unsafe"
# ones (Store, Edge, Terminal, Xbox identity/TCUI/speech, Get Help) are left
# out, because other apps and Windows features depend on them.
APPS: tuple[App, ...] = tuple(
    App(i, n, a, default=d, risk=SAFE if d else MODERATE) for i, n, a, d in (
        ("Microsoft.Copilot", "Copilot", "Microsoft's AI assistant app", True),
        ("Microsoft.Windows.AIHub", "AI Hub", "Copilot+ PC showcase app", True),
        ("Clipchamp.Clipchamp", "Clipchamp", "Video editor", True),
        ("Microsoft.3DBuilder", "3D Builder", "3D modelling", True),
        ("Microsoft.Microsoft3DViewer", "3D Viewer", "3D model viewer", True),
        ("Microsoft.Print3D", "Print 3D", "3D printing", True),
        ("Microsoft.MixedReality.Portal", "Mixed Reality Portal", "For VR headsets", True),
        ("Microsoft.549981C3F5F10", "Cortana", "Discontinued assistant", True),
        ("Microsoft.BingNews", "News", "Bing news feed", True),
        ("Microsoft.News", "Microsoft News", "News feed", True),
        ("Microsoft.BingWeather", "Weather", "Bing weather", True),
        ("Microsoft.BingFinance", "Money", "Discontinued Bing app", True),
        ("Microsoft.BingSports", "Sports", "Discontinued Bing app", True),
        ("Microsoft.BingTravel", "Travel", "Discontinued Bing app", True),
        ("Microsoft.BingFoodAndDrink", "Food & Drink", "Discontinued Bing app", True),
        ("Microsoft.BingHealthAndFitness", "Health & Fitness", "Discontinued Bing app", True),
        ("Microsoft.BingTranslator", "Translator", "Bing translator", True),
        ("Microsoft.Getstarted", "Tips", "Windows introduction", True),
        ("Microsoft.Messaging", "Messaging", "Discontinued", True),
        ("Microsoft.MicrosoftOfficeHub", "Office hub", "Microsoft 365 launcher and ads", True),
        ("Microsoft.MicrosoftSolitaireCollection", "Solitaire Collection", "Card games with ads", True),
        ("Microsoft.MicrosoftStickyNotes", "Sticky Notes", "Notes. Notes not synced to your account are deleted with it", False),
        ("Microsoft.MicrosoftJournal", "Journal", "Pen notes. Local notebooks are deleted with it", False),
        ("Microsoft.MicrosoftPowerBIForWindows", "Power BI", "Business analytics", True),
        ("Microsoft.NetworkSpeedTest", "Network Speed Test", "Speed test", True),
        ("Microsoft.Office.OneNote", "OneNote (UWP)", "Old OneNote app. Unsynced pages are deleted with it", False),
        ("Microsoft.Office.Sway", "Sway", "Presentations", True),
        ("Microsoft.OneConnect", "Mobile Plans", "Carrier app", True),
        ("Microsoft.People", "People", "Contacts", True),
        ("Microsoft.PowerAutomateDesktop", "Power Automate", "Automation. Local flows are deleted with it", False),
        ("Microsoft.SkypeApp", "Skype", "Discontinued", True),
        ("Microsoft.Todos", "To Do", "Task list (synced to your Microsoft account)", False),
        ("Microsoft.Windows.DevHome", "Dev Home", "Discontinued developer dashboard", True),
        ("Microsoft.WindowsAlarms", "Alarms & Clock", "Clock app. Your alarms and timers are deleted with it", False),
        ("Microsoft.WindowsFeedbackHub", "Feedback Hub", "Send feedback to Microsoft", True),
        ("Microsoft.WindowsMaps", "Maps", "Offline maps", True),
        ("Microsoft.WindowsSoundRecorder", "Sound Recorder", "Audio recorder", True),
        ("Microsoft.windowscommunicationsapps", "Mail and Calendar", "Discontinued; its local mail cache and account setup are deleted with it", False),
        ("Microsoft.XboxApp", "Xbox Console Companion", "Discontinued", True),
        ("Microsoft.ZuneVideo", "Movies & TV", "Video store", True),
        ("MicrosoftCorporationII.MicrosoftFamily", "Family Safety", "Family accounts", True),
        ("MicrosoftCorporationII.QuickAssist", "Quick Assist", "Remote help (scammers use it too)", True),
        ("MicrosoftTeams", "Teams (personal, old)", "Old Teams for home", True),
        ("Microsoft.PCManager", "PC Manager", "Microsoft's own cleaner", True),
        ("king.com.CandyCrushSaga", "Candy Crush Saga", "Preinstalled game", True),
        ("king.com.CandyCrushSodaSaga", "Candy Crush Soda", "Preinstalled game", True),
        ("king.com.BubbleWitch3Saga", "Bubble Witch 3", "Preinstalled game", True),
        ("SpotifyAB.SpotifyMusic", "Spotify", "Preinstalled", True),
        ("BytedancePte.Ltd.TikTok", "TikTok", "Preinstalled", True),
        ("Facebook.Instagram", "Instagram", "Preinstalled", True),
        ("FACEBOOK.FACEBOOK", "Facebook", "Preinstalled", True),
        ("4DF9E0F8.Netflix", "Netflix", "Preinstalled", True),
        ("Disney.37853FC22B2CE", "Disney+", "Preinstalled", True),
        ("AmazonVideo.PrimeVideo", "Prime Video", "Preinstalled", True),
        ("LinkedInforWindows", "LinkedIn", "Preinstalled", True),
        ("Microsoft.BingSearch", "Bing Search", "Web search in Windows Search", False),
        ("Microsoft.YourPhone", "Phone Link", "Connects your phone", False),
        ("Microsoft.GamingApp", "Xbox app", "Needed to install some PC Game Pass games", False),
        ("Microsoft.XboxGamingOverlay", "Xbox Game Bar", "Overlay, FPS counter, screen capture", False),
        ("Microsoft.OutlookForWindows", "Outlook (new)", "Mail client", False),
        ("Microsoft.ZuneMusic", "Media Player", "Music and video player", False),
        ("Microsoft.WindowsCamera", "Camera", "Camera app", False),
        ("Microsoft.Windows.Photos", "Photos", "Default image viewer", False),
        ("MicrosoftWindows.Client.WebExperience", "Widgets engine", "Powers Widgets", False),
        ("Microsoft.StartExperiencesApp", "Widgets feed", "Widgets' news feed", False),
    )
)


# ------------------------------------------------------------------ backends


class WinRegistry:
    """winreg, always in the 64-bit view: a 32-bit Python would otherwise write
    HKLM\\SOFTWARE into WOW6432Node, where Windows never reads these policies."""

    def __init__(self) -> None:
        import winreg

        self.w = winreg
        self.hives = {HKCU: winreg.HKEY_CURRENT_USER, HKLM: winreg.HKEY_LOCAL_MACHINE}
        self.view = winreg.KEY_WOW64_64KEY

    def key_exists(self, hive: str, key: str) -> bool:
        try:
            self.w.CloseKey(self.w.OpenKey(self.hives[hive], key, 0, self.w.KEY_READ | self.view))
            return True
        except OSError:
            return False

    def get(self, hive: str, key: str, name: str) -> tuple[int, object] | None:
        try:
            with self.w.OpenKey(self.hives[hive], key, 0, self.w.KEY_READ | self.view) as k:
                value, kind = self.w.QueryValueEx(k, name)
                return kind, value
        except OSError:
            return None

    def set(self, hive: str, key: str, name: str, kind: int, value) -> None:
        with self.w.CreateKeyEx(self.hives[hive], key, 0, self.w.KEY_SET_VALUE | self.view) as k:
            self.w.SetValueEx(k, name, 0, kind, value)

    def delete_value(self, hive: str, key: str, name: str) -> None:
        try:
            with self.w.OpenKey(self.hives[hive], key, 0, self.w.KEY_SET_VALUE | self.view) as k:
                self.w.DeleteValue(k, name)
        except FileNotFoundError:
            pass

    def delete_empty_key(self, hive: str, key: str) -> None:
        """Remove a key Cleam created, only if nothing else is in it."""
        try:
            with self.w.OpenKey(self.hives[hive], key, 0, self.w.KEY_READ | self.view) as k:
                subkeys, values, _ = self.w.QueryInfoKey(k)
            if subkeys == 0 and values == 0:
                self.w.DeleteKeyEx(self.hives[hive], key, self.view, 0)
        except OSError:
            pass


def _powershell(script: str, timeout: int = 120) -> str:
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _ps_list(items) -> str:
    return ",".join("'" + str(i).replace("'", "''") + "'" for i in items)


def _json_list(text: str) -> list:
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


class WinTasks:
    """Scheduled tasks through PowerShell. State is read as the TaskState enum's
    number (1 Disabled, 3 Ready...), never as text: schtasks prints it translated."""

    def states(self, paths: list[str]) -> dict[str, int]:
        if not paths:
            return {}
        script = (
            f"@({_ps_list(paths)}) | ForEach-Object {{ $i = $_.LastIndexOf('\\');"
            " $t = Get-ScheduledTask -TaskPath $_.Substring(0, $i + 1) -TaskName $_.Substring($i + 1)"
            " -ErrorAction SilentlyContinue;"
            " [ordered]@{ path = $_; state = if ($t) { [int]$t.State } else { -1 } } } | ConvertTo-Json -Compress"
        )
        return {r["path"]: int(r["state"]) for r in _json_list(_powershell(script)) if "path" in r}

    def set_enabled(self, path: str, enabled: bool) -> bool:
        i = path.rfind("\\")
        verb = "Enable-ScheduledTask" if enabled else "Disable-ScheduledTask"
        script = (f"try {{ {verb} -TaskPath {_ps_list([path[:i + 1]])} -TaskName {_ps_list([path[i + 1:]])}"
                  " -ErrorAction Stop | Out-Null; 'ok' } catch { 'fail' }")
        return _powershell(script).strip() == "ok"


class WinApps:
    """Appx packages of the current user. Removal is for this user only, so the
    copy Windows keeps staged stays, and undo can register it again."""

    def installed(self) -> dict[str, dict]:
        script = ("Get-AppxPackage | Select-Object Name, PackageFullName, PackageFamilyName, NonRemovable"
                  " | ConvertTo-Json -Compress")
        return {p["Name"]: p for p in _json_list(_powershell(script, timeout=180)) if isinstance(p, dict)}

    def remove(self, full_name: str) -> bool:
        script = f"try {{ Remove-AppxPackage -Package {_ps_list([full_name])} -ErrorAction Stop; 'ok' }} catch {{ 'fail' }}"
        return _powershell(script, timeout=300).strip() == "ok"

    def restore(self, family_name: str) -> bool:
        script = (f"try {{ Add-AppxPackage -RegisterByFamilyName -MainPackage {_ps_list([family_name])}"
                  " -ErrorAction Stop; 'ok' } catch { 'fail' }")
        return _powershell(script, timeout=300).strip() == "ok"


# ------------------------------------------------------------------ environment


@dataclass
class Env:
    build: int
    edition: str  # EditionID: Professional, Core (Home), Enterprise, Education...
    admin: bool

    @property
    def windows(self) -> str:
        return "11" if self.build >= 22000 else "10"


def current_env() -> Env:
    from .overview import _registry_version

    values = _registry_version() if OS == "windows" else {}
    return Env(int(values.get("CurrentBuild", 0) or 0), values.get("EditionID", ""), is_admin())


def edition_name(edition: str) -> str:
    return {"Core": "Home", "CoreSingleLanguage": "Home", "Professional": "Pro"}.get(edition, edition or "Windows")


def availability(tweak: Tweak, env: Env) -> str:
    """"" when the tweak can be applied here, else why not, in a few words."""
    if env.windows not in tweak.windows.split(","):
        return f"Windows {tweak.windows} only"
    if tweak.min_build and env.build < tweak.min_build:
        return f"needs build {tweak.min_build}+"
    if tweak.editions and not any(env.edition.startswith(e) for e in tweak.editions):
        return f"not on {edition_name(env.edition)} ({'/'.join(tweak.editions)} only)"
    if tweak.requires_file and not os.path.exists(os.path.expandvars(tweak.requires_file)):
        return "not installed"
    needs_action_admin = bool(tweak.action) and getattr(ACTIONS.get(tweak.action), "needs_admin", True)
    if not env.admin and (any(r.hive == HKLM for r in tweak.reg) or tweak.tasks or needs_action_admin):
        return "needs admin"
    return ""


# ------------------------------------------------------------------ journal


def journal_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return Path(base) / "Cleam" / "debloat" / "journal.json"


def load_journal(path: Path | None = None) -> dict:
    path = path or journal_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("tweaks", {})
    data.setdefault("apps", {})
    return data


def save_journal(data: dict, path: Path | None = None) -> None:
    path = path or journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # never a half-written journal: it is the only undo


# ------------------------------------------------------------------ the engine


@dataclass
class Outcome:
    id: str
    ok: bool
    message: str = ""
    restart: str = ""


@dataclass
class Debloater:
    registry: object = None
    tasks: object = None
    apps: object = None
    env: Env | None = None
    journal_file: Path | None = None
    actions: dict | None = None
    _installed: dict | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.registry is None:
            self.registry = WinRegistry()
        if self.tasks is None:
            self.tasks = WinTasks()
        if self.apps is None:
            self.apps = WinApps()
        if self.env is None:
            self.env = current_env()
        if self.actions is None:
            self.actions = ACTIONS

    # ---- state

    def _applicable(self, tweak: Tweak) -> list[Reg]:
        return [r for r in tweak.reg if not r.if_key_exists or self.registry.key_exists(r.hive, r.key)]

    def state(self, tweak: Tweak, task_states: dict[str, int] | None = None) -> str:
        """applied | partly | not applied | n/a"""
        checks: list[bool] = []
        for r in self._applicable(tweak):
            got = self.registry.get(r.hive, r.key, r.name)
            checks.append(got is not None and got[1] == r.value)
        if tweak.tasks:
            states = task_states if task_states is not None else self.tasks.states(list(tweak.tasks))
            checks += [states.get(t, -1) == 1 for t in tweak.tasks if states.get(t, -1) != -1]
        if tweak.action:
            done = self.actions[tweak.action].applied()
            if done is not None:  # None: this Windows has no such feature
                checks.append(done)
        if not checks:
            return "n/a"
        return "applied" if all(checks) else "partly" if any(checks) else "not applied"

    def states(self) -> dict[str, str]:
        """Every tweak's state, reading all scheduled tasks in one PowerShell call."""
        paths = [t for tweak in TWEAKS for t in tweak.tasks]
        task_states = self.tasks.states(paths) if paths and OS == "windows" else {}
        return {t.id: self.state(t, task_states) for t in TWEAKS}

    def installed_apps(self) -> dict[str, dict]:
        if self._installed is None:
            self._installed = self.apps.installed()
        return self._installed

    def removable_apps(self) -> list[tuple[App, dict]]:
        found = self.installed_apps()
        return [(a, found[a.id]) for a in APPS if a.id in found and not found[a.id].get("NonRemovable")]

    # ---- apply / undo

    def apply(self, tweak: Tweak) -> Outcome:
        why = availability(tweak, self.env)
        if why:
            return Outcome(tweak.id, False, why)
        journal = load_journal(self.journal_file)
        entry = journal["tweaks"].get(tweak.id)
        if entry is None:  # first application: this is the state to come back to
            entry = {"applied": time.strftime("%Y-%m-%d %H:%M:%S"), "registry": [], "tasks": []}
            for r in self._applicable(tweak):
                before = self.registry.get(r.hive, r.key, r.name)
                entry["registry"].append({
                    "hive": r.hive, "key": r.key, "name": r.name,
                    "key_existed": self.registry.key_exists(r.hive, r.key),
                    "kind": before[0] if before else None,
                    "value": before[1] if before else None,
                    "existed": before is not None,
                })
            states = self.tasks.states(list(tweak.tasks)) if tweak.tasks else {}
            entry["tasks"] = [{"path": p, "enabled": states[p] != 1} for p in tweak.tasks if states.get(p, -1) != -1]
            if tweak.action:
                entry["action_snapshot"] = self.actions[tweak.action].snapshot()
            journal["tweaks"][tweak.id] = entry
            save_journal(journal, self.journal_file)  # written before anything changes
        failed: list[str] = []
        for r in self._applicable(tweak):
            try:
                self.registry.set(r.hive, r.key, r.name, r.kind, r.value)
            except OSError as e:
                failed.append(f"{r.name}: {e.strerror or e}")
        for task in entry["tasks"]:
            if not self.tasks.set_enabled(task["path"], False):
                failed.append(f"task {task['path'].rsplit(chr(92), 1)[-1]}")
        if tweak.action:
            error = self.actions[tweak.action].apply()
            if error:
                failed.append(error)
        return Outcome(tweak.id, not failed, "; ".join(failed), tweak.restart)

    def undo(self, tweak_id: str) -> Outcome:
        journal = load_journal(self.journal_file)
        entry = journal["tweaks"].get(tweak_id)
        if entry is None:
            return Outcome(tweak_id, False, "not applied by Cleam")
        failed: list[str] = []
        for r in entry["registry"]:
            try:
                if r["existed"]:
                    self.registry.set(r["hive"], r["key"], r["name"], r["kind"], r["value"])
                else:
                    self.registry.delete_value(r["hive"], r["key"], r["name"])
                    if not r["key_existed"]:
                        # The classic context menu is switched on by the key's
                        # mere existence, so a key Cleam made has to go too.
                        self.registry.delete_empty_key(r["hive"], r["key"])
            except OSError as e:
                failed.append(f"{r['name']}: {e.strerror or e}")
        for task in entry["tasks"]:
            if task["enabled"] and not self.tasks.set_enabled(task["path"], True):
                failed.append(f"task {task['path'].rsplit(chr(92), 1)[-1]}")
        tweak = next((t for t in TWEAKS if t.id == tweak_id), None)
        if tweak and tweak.action:
            # Journals written before snapshots kept only "was it applied".
            snap = entry["action_snapshot"] if "action_snapshot" in entry else entry.get("action_was_applied")
            error = self.actions[tweak.action].undo(snap)
            if error:
                failed.append(error)
        if not failed:
            del journal["tweaks"][tweak_id]
            save_journal(journal, self.journal_file)
        return Outcome(tweak_id, not failed, "; ".join(failed), tweak.restart if tweak else "")

    def revert(self, tweak: Tweak) -> Outcome:
        """Turn a tweak off again, whoever turned it on.

        Cleam's own change is undone exactly from the journal. A change made
        by another tool (or by hand) has no before-value here, so it goes back
        to what Windows ships with: policy values removed, known defaults
        written back, tasks re-enabled, commands reversed.
        """
        if tweak.id in load_journal(self.journal_file)["tweaks"]:
            return self.undo(tweak.id)
        why = availability(tweak, self.env)
        if why:
            return Outcome(tweak.id, False, why)
        failed: list[str] = []
        for r in self._applicable(tweak):
            try:
                if r.original is None:
                    self.registry.delete_value(r.hive, r.key, r.name)
                else:
                    self.registry.set(r.hive, r.key, r.name, r.kind, r.original)
            except OSError as e:
                failed.append(f"{r.name}: {e.strerror or e}")
        states = self.tasks.states(list(tweak.tasks)) if tweak.tasks else {}
        for path in tweak.tasks:
            if states.get(path, -1) == 1 and not self.tasks.set_enabled(path, True):
                failed.append(f"task {path.rsplit(chr(92), 1)[-1]}")
        if tweak.action:
            error = self.actions[tweak.action].reset()
            if error:
                failed.append(error)
        return Outcome(tweak.id, not failed, "; ".join(failed), tweak.restart)

    def remove_app(self, app: App) -> Outcome:
        pkg = self.installed_apps().get(app.id)
        if not pkg:
            return Outcome(app.id, False, "not installed")
        journal = load_journal(self.journal_file)
        journal["apps"][app.id] = {"name": app.name, "family": pkg["PackageFamilyName"],
                                   "removed": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_journal(journal, self.journal_file)
        if not self.apps.remove(pkg["PackageFullName"]):
            del journal["apps"][app.id]
            save_journal(journal, self.journal_file)
            return Outcome(app.id, False, "Windows refused to remove it")
        self._installed = None
        return Outcome(app.id, True)

    def restore_app(self, app_id: str) -> Outcome:
        journal = load_journal(self.journal_file)
        entry = journal["apps"].get(app_id)
        if entry is None:
            return Outcome(app_id, False, "not removed by Cleam")
        if not self.apps.restore(entry["family"]):
            return Outcome(app_id, False, f"reinstall {entry['name']} from the Microsoft Store")
        del journal["apps"][app_id]
        save_journal(journal, self.journal_file)
        self._installed = None
        return Outcome(app_id, True)

    def applied(self) -> dict:
        """What Cleam changed and can undo: {"tweaks": {...}, "apps": {...}}."""
        return load_journal(self.journal_file)


def _explorer_running() -> bool:
    out = subprocess.run(["tasklist", "/fi", "imagename eq explorer.exe", "/fo", "csv", "/nh"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return "explorer.exe" in out.lower()


class Action:
    """A change made through a Windows command or a value that must be merged.

    applied()   is it in effect? None when this Windows lacks the feature.
    snapshot()  the exact state to come back to, saved in the journal first.
    apply()     make the change; "" on success, else why not.
    undo(snap)  put back exactly what snapshot() saw.
    reset()     put back what Windows ships with (for revert without a journal).
    """

    needs_admin = True

    def snapshot(self):
        return self.applied()


class ReservedStorage(Action):
    """Windows' own cmdlets; their State is an enum, so the text is never translated."""

    def applied(self) -> bool | None:
        out = _powershell("try { \"$((Get-WindowsReservedStorageState -ErrorAction Stop).ReservedStorageState)\" }"
                          " catch { 'none' }").strip()
        return None if out in ("", "none") else out == "Disabled"

    def _set(self, state: str) -> str:
        out = _powershell(f"try {{ Set-WindowsReservedStorageState -State {state} -ErrorAction Stop; 'ok' }}"
                          " catch { $_.Exception.Message }").strip()
        # Refused while an update is installing ("reserved storage is in use").
        return "" if out.endswith("ok") else (out or "Windows refused")

    def apply(self) -> str:
        return self._set("Disabled")

    def undo(self, snap) -> str:
        return self._set("Enabled") if snap is False else ""

    def reset(self) -> str:
        return self._set("Enabled")


class Hibernation(Action):
    """powercfg writes HibernateEnabled and deletes or recreates hiberfil.sys."""

    KEY = r"SYSTEM\CurrentControlSet\Control\Power"

    def applied(self) -> bool | None:
        if OS != "windows":
            return None
        got = WinRegistry().get(HKLM, self.KEY, "HibernateEnabled")
        return None if got is None else got[1] == 0

    @staticmethod
    def _powercfg(*args: str) -> str:
        try:
            done = subprocess.run(["powercfg", *args], capture_output=True, text=True,
                                  creationflags=NO_WINDOW, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)
        return "" if done.returncode == 0 else (done.stdout + done.stderr).strip() or "powercfg failed"

    def apply(self) -> str:
        return self._powercfg("/hibernate", "off")

    def undo(self, snap) -> str:
        return self._powercfg("/hibernate", "on") if snap is False else ""

    def reset(self) -> str:
        return self._powercfg("/hibernate", "on")


class PowerPlan(Action):
    """The active power scheme, by GUID: powercfg's GUIDs are not translated, its names are."""

    BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
    SAVER = "a1841308-3541-4fab-bc81-f71556f20b4a"
    HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

    def active(self) -> str | None:
        if OS != "windows":
            return None
        try:
            out = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        import re

        m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", out)
        return m.group().lower() if m else None

    def applied(self) -> bool | None:
        guid = self.active()
        return None if guid is None else guid not in (self.BALANCED, self.SAVER)

    def snapshot(self):
        return self.active()

    def apply(self) -> str:
        if self.applied():
            return ""  # already on a performance plan: keep the user's own
        # Hidden on some Modern Standby laptops, where setactive then fails.
        return Hibernation._powercfg("/setactive", self.HIGH)

    def undo(self, snap) -> str:
        return Hibernation._powercfg("/setactive", snap) if snap else ""

    def reset(self) -> str:
        return Hibernation._powercfg("/setactive", self.BALANCED)


class OptionalFeature(Action):
    """A Windows optional feature; State is an enum, so it is read as text safely."""

    def __init__(self, name: str) -> None:
        self.name = name

    def applied(self) -> bool | None:
        out = _powershell(f"try {{ \"$((Get-WindowsOptionalFeature -Online -FeatureName {self.name}"
                          " -ErrorAction Stop).State)\" } catch { 'none' }", timeout=180).strip()
        return None if out in ("", "none") else out.startswith("Disabled")

    def _set(self, verb: str) -> str:
        out = _powershell(f"try {{ {verb}-WindowsOptionalFeature -Online -FeatureName {self.name} -NoRestart"
                          " -ErrorAction Stop | Out-Null; 'ok' } catch { $_.Exception.Message }", timeout=600).strip()
        return "" if out.endswith("ok") else (out or "Windows refused")

    def apply(self) -> str:
        return self._set("Disable")

    def undo(self, snap) -> str:
        return self._set("Enable") if snap is False else ""

    def reset(self) -> str:
        return self._set("Enable")


class RegistryField(Action):
    """One setting inside a value that holds several.

    DirectXUserGlobalSettings is "SwapEffectUpgradeEnable=1;VRROptimizeEnable=0;"
    -- the windowed-games switch shares it with VRR and Auto HDR, so writing
    the whole string (as tweak guides do) would flip those too. StickyKeys'
    Flags is a bitmask of which the hotkey is one bit; writing a fixed number
    (the usual "506") would also switch Sticky Keys itself off for someone
    who relies on it. Both are read, changed in place, and written back.
    """

    needs_admin = False

    def __init__(self, key: str, name: str, on, off, is_on, registry=None) -> None:
        self.key, self.name = key, name
        self._on, self._off, self._is_on = on, off, is_on
        self._registry = registry

    @property
    def reg(self):
        if self._registry is None:
            self._registry = WinRegistry()
        return self._registry

    def _get(self) -> str | None:
        got = self.reg.get(HKCU, self.key, self.name)
        return None if got is None else str(got[1])

    def applied(self) -> bool | None:
        if OS != "windows" and self._registry is None:
            return None
        return self._is_on(self._get())

    def snapshot(self):
        return self._get()

    def _write(self, text: str | None) -> str:
        try:
            if text is None:
                self.reg.delete_value(HKCU, self.key, self.name)
            else:
                self.reg.set(HKCU, self.key, self.name, SZ, text)
        except OSError as e:
            return str(e.strerror or e)
        return ""

    def apply(self) -> str:
        return self._write(self._on(self._get()))

    def undo(self, snap) -> str:
        return self._write(snap)

    def reset(self) -> str:
        return self._write(self._off(self._get()))


def _dx_pairs(text: str | None) -> dict[str, str]:
    pairs = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            pairs[k.strip()] = v.strip()
    return pairs


def _dx_join(pairs: dict[str, str]) -> str | None:
    return "".join(f"{k}={v};" for k, v in pairs.items()) or None


def _dx_set(text: str | None, value: str | None) -> str | None:
    pairs = _dx_pairs(text)
    if value is None:
        pairs.pop("SwapEffectUpgradeEnable", None)
    else:
        pairs["SwapEffectUpgradeEnable"] = value
    return _dx_join(pairs)


STICKY_HOTKEY = 0x4  # SKF_HOTKEYACTIVE: five Shift presses open the prompt


def _flags(text: str | None) -> int:
    try:
        return int(text or "510")  # 510 is what Windows ships with
    except ValueError:
        return 510


ACTIONS = {
    "reserved-storage": ReservedStorage(),
    "hibernation": Hibernation(),
    "power-plan": PowerPlan(),
    "virtual-machine-platform": OptionalFeature("VirtualMachinePlatform"),
    "windowed-games": RegistryField(
        r"Software\Microsoft\DirectX\UserGpuPreferences", "DirectXUserGlobalSettings",
        on=lambda t: _dx_set(t, "1"), off=lambda t: _dx_set(t, None),
        is_on=lambda t: _dx_pairs(t).get("SwapEffectUpgradeEnable") == "1"),
    "sticky-keys-hotkey": RegistryField(
        r"Control Panel\Accessibility\StickyKeys", "Flags",
        on=lambda t: str(_flags(t) & ~STICKY_HOTKEY), off=lambda t: str(_flags(t) | STICKY_HOTKEY),
        is_on=lambda t: not _flags(t) & STICKY_HOTKEY),
}


def restart_explorer() -> None:
    """Most taskbar and Explorer tweaks show only after Explorer restarts.

    Never started from here directly: Cleam usually runs elevated, and an
    Explorer started by an elevated process can become an elevated desktop
    shell on Windows 10 -- every program opened from the taskbar would then
    run as administrator. Windows restarts a killed shell by itself
    (AutoRestartShell), as the user; only if it has not come back is it
    started with a basic-user token (runas /trustlevel:0x20000).
    """
    subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], capture_output=True, creationflags=NO_WINDOW)
    for _ in range(16):
        time.sleep(0.5)
        if _explorer_running():
            return
    subprocess.run(["runas", "/trustlevel:0x20000", "explorer.exe"], capture_output=True, creationflags=NO_WINDOW)


def recommended(env: Env) -> list[Tweak]:
    return [t for t in TWEAKS if t.default and not availability(t, env)]


def as_dict(tweak: Tweak) -> dict:
    return asdict(tweak)

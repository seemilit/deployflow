param([int]$X, [int]$Y, [long]$Window, [int]$Desktop)

$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName WindowsBase
    $shell = New-Object -ComObject Shell.Application
    $folders = @()
    if ($Desktop) {
        $folders = @($shell.NameSpace(0))
    } else {
        foreach ($view in $shell.Windows()) {
            if ([long]$view.HWND -eq $Window) {
                $folders += $view.Document.Folder
            }
        }
    }
    if ($folders.Count -ne 1) { throw 'Ambiguous or missing Explorer folder' }
    $folder = $folders[0]
    $element = [System.Windows.Automation.AutomationElement]::FromPoint(
        [System.Windows.Point]::new($X, $Y)
    )
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $itemName = $null
    $insideFiles = $false
    $navigationItem = $false
    $owner = 0
    for ($depth = 0; $null -ne $element -and $depth -lt 40; $depth++) {
        $current = $element.Current
        $type = $current.ControlType
        if ($type -eq [System.Windows.Automation.ControlType]::TreeItem) {
            $navigationItem = $true
        }
        if ($null -eq $itemName -and (
            $type -eq [System.Windows.Automation.ControlType]::ListItem -or
            $type -eq [System.Windows.Automation.ControlType]::DataItem
        )) { $itemName = $current.Name }
        if ($current.ClassName -in @('UIItemsView', 'SysListView32') -or
            $current.AutomationId -in @('ItemsView', 'ItemView')) {
            $insideFiles = $true
        }
        if ($type -eq [System.Windows.Automation.ControlType]::Window) {
            $owner = $current.NativeWindowHandle
            break
        }
        $element = $walker.GetParent($element)
    }
    if (-not $insideFiles -or $navigationItem) { throw 'Drop outside the file list' }
    if (-not $Desktop -and $owner -ne $Window) { throw 'Explorer window changed' }
    $target = [string]$folder.Self.Path
    if ($null -ne $itemName) {
        $item = $folder.ParseName($itemName)
        if ($null -eq $item) {
            foreach ($candidate in $folder.Items()) {
                if ($candidate.Name -ceq $itemName) {
                    if ($null -ne $item) { throw 'Ambiguous hovered item' }
                    $item = $candidate
                }
            }
        }
        if ($null -eq $item) { throw 'Cannot resolve hovered item' }
        if ($item.IsFolder) { $target = [string]$item.Path }
    }
    if (-not [System.IO.Directory]::Exists($target)) { throw 'Not a filesystem folder' }
    [Console]::Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($target)))
} catch {
    exit 1
}

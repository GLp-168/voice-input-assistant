[CmdletBinding()]
param(
    [string]$StagePath,
    [string]$DistPath,
    [ValidateRange(1, 1000)]
    [int]$TopN = 15
)

$ErrorActionPreference = 'Stop'

$scriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$script:RepoRoot = Split-Path -Parent $scriptRoot

function Format-Bytes {
    param([Int64]$Bytes)

    $units = @('B', 'KiB', 'MiB', 'GiB', 'TiB')
    $value = [double]$Bytes
    $unitIndex = 0

    while (($value -ge 1024) -and ($unitIndex -lt ($units.Count - 1))) {
        $value = $value / 1024
        $unitIndex++
    }

    if ($unitIndex -eq 0) {
        return ("{0} {1}" -f $Bytes, $units[$unitIndex])
    }

    return ("{0:N2} {1}" -f $value, $units[$unitIndex])
}

function Resolve-InputPath {
    param([string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }

    $location = Get-Location
    $currentRoot = $location.ProviderPath
    if ([string]::IsNullOrWhiteSpace($currentRoot)) {
        $currentRoot = $location.Path
    }

    $currentCandidate = [System.IO.Path]::GetFullPath((Join-Path $currentRoot $Path))
    if (Test-Path -LiteralPath $currentCandidate) {
        return $currentCandidate
    }

    return [System.IO.Path]::GetFullPath((Join-Path $script:RepoRoot $Path))
}

function Split-FirstPathPart {
    param([string]$RelativePath)

    $trimmed = $RelativePath.TrimStart([char[]]"\/")
    if ([string]::IsNullOrEmpty($trimmed)) {
        return [pscustomobject][ordered]@{
            First = ''
            Remainder = ''
            HasRemainder = $false
        }
    }

    $backslashIndex = $trimmed.IndexOf([char]'\')
    $slashIndex = $trimmed.IndexOf([char]'/')
    $separatorIndex = -1

    if (($backslashIndex -ge 0) -and ($slashIndex -ge 0)) {
        $separatorIndex = [Math]::Min($backslashIndex, $slashIndex)
    } elseif ($backslashIndex -ge 0) {
        $separatorIndex = $backslashIndex
    } elseif ($slashIndex -ge 0) {
        $separatorIndex = $slashIndex
    }

    if ($separatorIndex -lt 0) {
        return [pscustomobject][ordered]@{
            First = $trimmed
            Remainder = ''
            HasRemainder = $false
        }
    }

    return [pscustomobject][ordered]@{
        First = $trimmed.Substring(0, $separatorIndex)
        Remainder = $trimmed.Substring($separatorIndex + 1)
        HasRemainder = $true
    }
}

function New-SizeRow {
    param(
        [string]$Kind,
        [string]$Name,
        [Int64]$Bytes
    )

    return [pscustomobject][ordered]@{
        Kind = $Kind
        Size = Format-Bytes $Bytes
        Bytes = $Bytes
        Name = $Name
    }
}

function Add-SizeToMap {
    param(
        [hashtable]$Map,
        [string]$Key,
        [string]$Kind,
        [string]$Name,
        [Int64]$Bytes
    )

    if (-not $Map.ContainsKey($Key)) {
        $Map[$Key] = [pscustomobject][ordered]@{
            Kind = $Kind
            Name = $Name
            Bytes = [Int64]0
        }
    }

    $Map[$Key].Bytes = [Int64]($Map[$Key].Bytes) + $Bytes
}

function Convert-MapToSizeRows {
    param([hashtable]$Map)

    $rows = @()
    foreach ($entry in $Map.Values) {
        $rows += New-SizeRow -Kind $entry.Kind -Name $entry.Name -Bytes ([Int64]$entry.Bytes)
    }

    return @($rows | Sort-Object -Property Bytes -Descending)
}

function Get-TreeSizeBytes {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [Int64]0
    }

    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) {
        return [Int64]$item.Length
    }

    $total = [Int64]0
    foreach ($file in Get-ChildItem -LiteralPath $Path -Force -Recurse -File -ErrorAction SilentlyContinue) {
        $total += [Int64]$file.Length
    }

    return $total
}

function Get-DistArtifactRows {
    param([string]$DistRoot)

    if (-not (Test-Path -LiteralPath $DistRoot)) {
        return @()
    }

    $rows = @()
    foreach ($item in Get-ChildItem -LiteralPath $DistRoot -Force -ErrorAction SilentlyContinue) {
        if ($item.PSIsContainer) {
            $rows += New-SizeRow -Kind 'Dir' -Name $item.Name -Bytes (Get-TreeSizeBytes $item.FullName)
        } else {
            $rows += New-SizeRow -Kind 'File' -Name $item.Name -Bytes ([Int64]$item.Length)
        }
    }

    return @($rows | Sort-Object -Property Bytes -Descending)
}

function Get-StageAnalysis {
    param([string]$StageRoot)

    $sectionNames = @('models', '_internal', 'tools', 'docs')
    $sectionSizes = [ordered]@{}
    $sectionKinds = [ordered]@{}

    foreach ($sectionName in $sectionNames) {
        $sectionPath = Join-Path $StageRoot $sectionName
        $sectionSizes[$sectionName] = [Int64]0
        if (Test-Path -LiteralPath $sectionPath) {
            $sectionItem = Get-Item -LiteralPath $sectionPath -Force
            if ($sectionItem.PSIsContainer) {
                $sectionKinds[$sectionName] = 'Dir'
            } else {
                $sectionKinds[$sectionName] = 'File'
            }
        } else {
            $sectionKinds[$sectionName] = 'Missing'
        }
    }

    if (-not (Test-Path -LiteralPath $StageRoot)) {
        return [pscustomobject][ordered]@{
            Exists = $false
            TotalBytes = [Int64]0
            FileCount = [Int64]0
            SectionRows = @()
            InternalRows = @()
            FunasrRows = @()
        }
    }

    $stageItem = Get-Item -LiteralPath $StageRoot -Force
    if (-not $stageItem.PSIsContainer) {
        return [pscustomobject][ordered]@{
            Exists = $true
            TotalBytes = [Int64]$stageItem.Length
            FileCount = [Int64]1
            SectionRows = @()
            InternalRows = @()
            FunasrRows = @()
        }
    }

    $stagePrefix = $stageItem.FullName.TrimEnd([char[]]"\/") + [System.IO.Path]::DirectorySeparatorChar
    $totalBytes = [Int64]0
    $fileCount = [Int64]0
    $internalMap = @{}
    $funasrMap = @{}

    foreach ($file in Get-ChildItem -LiteralPath $stageItem.FullName -Force -Recurse -File -ErrorAction SilentlyContinue) {
        $length = [Int64]$file.Length
        $totalBytes += $length
        $fileCount++

        if (-not $file.FullName.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }

        $relative = $file.FullName.Substring($stagePrefix.Length)
        $first = Split-FirstPathPart $relative

        foreach ($sectionName in $sectionNames) {
            if ([string]::Equals($first.First, $sectionName, [StringComparison]::OrdinalIgnoreCase)) {
                $sectionSizes[$sectionName] = [Int64]($sectionSizes[$sectionName]) + $length
                break
            }
        }

        if ([string]::Equals($first.First, '_internal', [StringComparison]::OrdinalIgnoreCase)) {
            $internalChild = Split-FirstPathPart $first.Remainder
            if (-not [string]::IsNullOrEmpty($internalChild.First)) {
                $kind = 'File'
                if ($internalChild.HasRemainder) {
                    $kind = 'Dir'
                }
                $displayName = "_internal\$($internalChild.First)"
                $key = ("internal|{0}|{1}" -f $kind, $internalChild.First.ToLowerInvariant())
                Add-SizeToMap -Map $internalMap -Key $key -Kind $kind -Name $displayName -Bytes $length
            }
        }

        if ([string]::Equals($first.First, 'models', [StringComparison]::OrdinalIgnoreCase)) {
            $modelsChild = Split-FirstPathPart $first.Remainder
            if ([string]::Equals($modelsChild.First, 'funasr', [StringComparison]::OrdinalIgnoreCase)) {
                $modelChild = Split-FirstPathPart $modelsChild.Remainder
                if (-not [string]::IsNullOrEmpty($modelChild.First)) {
                    $kind = 'File'
                    if ($modelChild.HasRemainder) {
                        $kind = 'Dir'
                    }
                    $displayName = "models\funasr\$($modelChild.First)"
                    $key = ("funasr|{0}|{1}" -f $kind, $modelChild.First.ToLowerInvariant())
                    Add-SizeToMap -Map $funasrMap -Key $key -Kind $kind -Name $displayName -Bytes $length
                }
            }
        }
    }

    $sectionRows = @()
    foreach ($sectionName in $sectionNames) {
        $sectionRows += New-SizeRow -Kind $sectionKinds[$sectionName] -Name $sectionName -Bytes ([Int64]$sectionSizes[$sectionName])
    }

    return [pscustomobject][ordered]@{
        Exists = $true
        TotalBytes = $totalBytes
        FileCount = $fileCount
        SectionRows = @($sectionRows | Sort-Object -Property Bytes -Descending)
        InternalRows = @(Convert-MapToSizeRows -Map $internalMap)
        FunasrRows = @(Convert-MapToSizeRows -Map $funasrMap)
    }
}

function Write-SizeRows {
    param(
        [object[]]$Rows,
        [int]$First = 0
    )

    $items = @($Rows)
    if ($First -gt 0) {
        $items = @($items | Select-Object -First $First)
    }

    if ($items.Count -eq 0) {
        Write-Output "  (none)"
        return
    }

    Write-Output ("{0,-8} {1,12} {2,15}  {3}" -f 'Kind', 'Size', 'Bytes', 'Name')
    Write-Output ("{0,-8} {1,12} {2,15}  {3}" -f '----', '----', '-----', '----')
    foreach ($row in $items) {
        Write-Output ("{0,-8} {1,12} {2,15}  {3}" -f $row.Kind, $row.Size, $row.Bytes, $row.Name)
    }
}

if ([string]::IsNullOrWhiteSpace($StagePath)) {
    $StagePath = Join-Path $script:RepoRoot 'build\windows\stage\VoiceInputAssistant'
}
if ([string]::IsNullOrWhiteSpace($DistPath)) {
    $DistPath = Join-Path $script:RepoRoot 'dist\windows'
}

$stageFullPath = Resolve-InputPath $StagePath
$distFullPath = Resolve-InputPath $DistPath

Write-Output 'Package size report'
Write-Output ("Repo root : {0}" -f $script:RepoRoot)
Write-Output ("Stage     : {0}" -f $stageFullPath)
Write-Output ("Dist      : {0}" -f $distFullPath)
Write-Output ("Top N     : {0}" -f $TopN)
Write-Output ''

Write-Output '== Dist artifacts =='
if (-not (Test-Path -LiteralPath $distFullPath)) {
    Write-Output '  (dist path missing)'
} else {
    $distRows = @(Get-DistArtifactRows -DistRoot $distFullPath)
    $distTotal = [Int64]0
    foreach ($row in $distRows) {
        $distTotal += [Int64]$row.Bytes
    }
    Write-Output ("Artifact total: {0} ({1} bytes)" -f (Format-Bytes $distTotal), $distTotal)
    Write-SizeRows -Rows $distRows
}
Write-Output ''

Write-Output '== Stage summary =='
$stage = Get-StageAnalysis -StageRoot $stageFullPath
if (-not $stage.Exists) {
    Write-Output '  (stage path missing)'
} else {
    Write-Output ("Stage total: {0} ({1} bytes)" -f (Format-Bytes ([Int64]$stage.TotalBytes)), $stage.TotalBytes)
    Write-Output ("File count : {0}" -f $stage.FileCount)
    Write-Output ''
    Write-Output 'Top-level package areas'
    Write-SizeRows -Rows $stage.SectionRows
    Write-Output ''
    Write-Output ("_internal top {0}" -f $TopN)
    Write-SizeRows -Rows $stage.InternalRows -First $TopN
    Write-Output ''
    Write-Output 'models\funasr models'
    Write-SizeRows -Rows $stage.FunasrRows
}

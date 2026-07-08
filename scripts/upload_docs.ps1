param(
    [string]$BaseUrl = "http://10.32.11.17:8100",
    [string]$DocsDir = (Join-Path $PSScriptRoot "..\docs_data"),
    [string]$Corpus = "norm",
    [string]$DocType = "regulation",
    [string]$Lang = "ru",
    [string]$Pattern = "*.docx",
    [int]$PollSeconds = 5,
    [int]$JobTimeoutMinutes = 180,
    [bool]$NameFromFile = $true,
    [bool]$VersionFromFile = $true,
    [switch]$StopOnError
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Net.Http

function Join-Url([string]$Base, [string]$Path) {
    return $Base.TrimEnd("/") + "/" + $Path.TrimStart("/")
}

function Get-JobField($Job, [string]$Name) {
    # Safe read under Set-StrictMode: the status DTO may omit stage fields while queued.
    $prop = $Job.PSObject.Properties[$Name]
    if ($prop) { return $prop.Value }
    return $null
}

function Get-DocumentName([System.IO.FileInfo]$File) {
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($File.Name)
    $normalized = (($stem -replace "_", " ") -replace "\s+", " ").Trim()
    return $normalized
}

function Get-DocumentVersion([System.IO.FileInfo]$File) {
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($File.Name)
    $match = [regex]::Match($stem, "(19|20)\d{2}")
    if ($match.Success) {
        return $match.Value
    }
    return $null
}

function Invoke-Upload([System.IO.FileInfo]$File) {
    # NOTE: Uses curl.exe (bundled with Windows 10/11) for the multipart POST. The .NET Framework
    # System.Net.Http.MultipartFormDataContent in Windows PowerShell 5.1 mangles/drops the file
    # name for non-ASCII (Cyrillic) filenames, so the server sees an empty extension and rejects
    # every upload with HTTP 415. curl builds the Content-Disposition header correctly.
    $url = Join-Url $BaseUrl "documents"
    $ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    $curlArgs = @(
        "-s", "-S",
        "-X", "POST", $url,
        "-F", "file=@$($File.FullName);type=$ctype",
        "-F", "corpus=$Corpus",
        "-F", "doc_type=$DocType",
        "-F", "lang=$Lang",
        "-F", "source_uri=$($File.Name)"
    )
    if ($NameFromFile) {
        $curlArgs += @("-F", "name=$(Get-DocumentName $File)")
    }
    if ($VersionFromFile) {
        $version = Get-DocumentVersion $File
        if ($version) {
            $curlArgs += @("-F", "version=$version")
        }
    }

    $text = & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) {
        throw "curl transport error (exit $LASTEXITCODE): $text"
    }

    try {
        $obj = $text | ConvertFrom-Json
    }
    catch {
        throw "unexpected response: $text"
    }
    if (-not $obj.job_id) {
        throw "upload failed: $text"
    }
    return $obj
}

function Wait-Job([string]$JobId, [System.IO.FileInfo]$File, [int]$Index, [int]$Total) {
    $url = Join-Url $BaseUrl "documents/$JobId"
    $deadline = (Get-Date).AddMinutes($JobTimeoutMinutes)
    $lastDetail = $null

    while ($true) {
        if ((Get-Date) -gt $deadline) {
            throw "job timeout after $JobTimeoutMinutes minutes: $JobId"
        }

        $job = Invoke-RestMethod -Method GET -Uri $url -TimeoutSec 60
        $status = [string]$job.status

        # Live per-document progress published by the server (see JobStatusDTO): the current
        # pipeline stage plus an in-stage item counter for the chunked stages.
        $stage = Get-JobField $job "stage"
        $stageIndex = Get-JobField $job "stage_index"
        $stageTotal = Get-JobField $job "stage_total"
        $done = Get-JobField $job "progress"
        $doneTotal = Get-JobField $job "progress_total"

        # Fraction of THIS document completed: whole stages done + the counter inside the
        # current one (each stage weighs 1/stageTotal of the document).
        $docFraction = 0.0
        if ($stageTotal) {
            $docFraction = ($stageIndex - 1) / $stageTotal
            if ($doneTotal) {
                $docFraction += ($done / $doneTotal) / $stageTotal
            }
        }
        $docFraction = [Math]::Min(1.0, [Math]::Max(0.0, $docFraction))

        # Human-readable detail, e.g. "stage 7/8 embeddings · 128/256".
        $detail = $status
        if ($stage) {
            $detail = "stage $stageIndex/$stageTotal $stage"
            if ($doneTotal) {
                $detail += " · $done/$doneTotal"
            }
        }

        # Two bars: parent (Id 1) tracks the whole run, child (Id 2) the current document's
        # pipeline. The overall percent blends completed files with the current doc's fraction.
        $overall = [int]((($Index - 1) + $docFraction) * 100 / [Math]::Max($Total, 1))
        Write-Progress -Id 1 `
            -Activity "Uploading docs_data to $BaseUrl" `
            -Status "[$Index/$Total] $($File.Name)" `
            -PercentComplete $overall
        Write-Progress -Id 2 -ParentId 1 `
            -Activity $File.Name `
            -Status $detail `
            -PercentComplete ([int]($docFraction * 100))

        # Echo each new stage to the console too — Write-Progress is invisible in captured logs.
        if ($stage -and $detail -ne $lastDetail) {
            Write-Host "    $detail"
            $lastDetail = $detail
        }

        if ($status -eq "done") {
            Write-Progress -Id 2 -ParentId 1 -Activity $File.Name -Completed
            return $job
        }
        if ($status -eq "error") {
            Write-Progress -Id 2 -ParentId 1 -Activity $File.Name -Completed
            throw "job failed: $($job.error)"
        }

        Start-Sleep -Seconds $PollSeconds
    }
}

$resolvedDocsDir = Resolve-Path -LiteralPath $DocsDir
$files = @(Get-ChildItem -LiteralPath $resolvedDocsDir -Recurse -File -Filter $Pattern | Sort-Object FullName)
if ($files.Count -eq 0) {
    throw "No files matching '$Pattern' found in '$resolvedDocsDir'"
}

$pingUrl = Join-Url $BaseUrl "ping"
Invoke-RestMethod -Method GET -Uri $pingUrl -TimeoutSec 15 | Out-Null

Write-Host "Base URL: $BaseUrl"
Write-Host "Docs dir: $resolvedDocsDir"
Write-Host "Files: $($files.Count)"
Write-Host "Corpus: $Corpus"
Write-Host "Doc type: $DocType"
Write-Host "Lang: $Lang"
Write-Host "Note: Qdrant collection is selected by the running server configuration; this script sets the logical corpus."
Write-Host ""

$results = New-Object System.Collections.Generic.List[object]
$failures = New-Object System.Collections.Generic.List[object]

for ($i = 0; $i -lt $files.Count; $i++) {
    $file = $files[$i]
    $index = $i + 1
    $percent = [int](($i) * 100 / [Math]::Max($files.Count, 1))
    Write-Progress -Id 1 -Activity "Uploading docs_data to $BaseUrl" -Status "[$index/$($files.Count)] $($file.Name): upload" -PercentComplete $percent
    Write-Host "[$index/$($files.Count)] Uploading $($file.Name)"

    try {
        $upload = Invoke-Upload $file
        Write-Host "  job: $($upload.job_id)"
        $job = Wait-Job $upload.job_id $file $index $files.Count
        Write-Host "  done: name='$($job.name)' version='$($job.version)' nodes=$($job.nodes)"
        $results.Add([pscustomobject]@{
            file = $file.Name
            job_id = $upload.job_id
            status = "done"
            name = $job.name
            version = $job.version
            nodes = $job.nodes
        }) | Out-Null
    }
    catch {
        $message = [string]$_.Exception.Message
        Write-Host "  failed: $message" -ForegroundColor Red
        $failures.Add([pscustomobject]@{
            file = $file.Name
            error = $message
        }) | Out-Null
        if ($StopOnError) {
            break
        }
    }
}

Write-Progress -Id 1 -Activity "Uploading docs_data to $BaseUrl" -Completed
Write-Host ""
Write-Host "Summary: done=$($results.Count), failed=$($failures.Count), total=$($files.Count)"

if ($results.Count -gt 0) {
    Write-Host ""
    Write-Host "Uploaded:"
    $results | Format-Table -AutoSize
}

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host "Failures:"
    $failures | Format-Table -AutoSize
    exit 1
}

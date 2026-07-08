param(
    [string]$BaseUrl = "http://localhost:8100",
    [string]$DocsDir = (Join-Path $PSScriptRoot "..\docs_data"),
    [string]$Corpus = "norm",
    [string]$DocType = "regulation",
    [string]$Lang = "ru",
    [string]$Pattern = "*.docx",
    [int]$PollSeconds = 5,
    [int]$Prefetch = 2,
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

function Get-JobProgress($Job) {
    # Turn a job-status response into { Status; Fraction (0..1); Detail } for display.
    # Fields come from the server (see JobStatusDTO): the current pipeline stage plus an
    # in-stage item counter for the chunked stages.
    $status = [string]$Job.status
    $stage = Get-JobField $Job "stage"
    $stageIndex = Get-JobField $Job "stage_index"
    $stageTotal = Get-JobField $Job "stage_total"
    $done = Get-JobField $Job "progress"
    $doneTotal = Get-JobField $Job "progress_total"

    # Fraction of THIS document completed: whole stages done + the counter inside the current
    # one (each stage weighs 1/stageTotal of the document).
    $fraction = 0.0
    if ($status -eq "done") {
        $fraction = 1.0
    }
    elseif ($stageTotal) {
        $fraction = ($stageIndex - 1) / $stageTotal
        if ($doneTotal) {
            $fraction += ($done / $doneTotal) / $stageTotal
        }
    }
    $fraction = [Math]::Min(1.0, [Math]::Max(0.0, $fraction))

    # Human-readable detail, e.g. "stage 7/8 embeddings · 128/256"; "queued" while waiting
    # for a free GPU slot on the server.
    $detail = $status
    if ($status -eq "processing" -and $stage) {
        $detail = "stage $stageIndex/$stageTotal $stage"
        if ($doneTotal) {
            $detail += " · $done/$doneTotal"
        }
    }

    return [pscustomobject]@{ Status = $status; Fraction = $fraction; Detail = $detail }
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
Write-Host "Prefetch: $Prefetch (documents submitted ahead; the server runs one on the GPU at a time)"
Write-Host "Note: Qdrant collection is selected by the running server configuration; this script sets the logical corpus."
Write-Host ""

$results = New-Object System.Collections.Generic.List[object]
$failures = New-Object System.Collections.Generic.List[object]
$inflight = New-Object System.Collections.Generic.List[object]
$next = 0            # index of the next file to submit
$completed = 0       # files finished (done or failed)
$stopSubmitting = $false

# Pipeline the batch: keep up to $Prefetch documents submitted ahead so the next one is already
# parsed and queued on the server, ready to grab the GPU the instant the current document frees
# it. The server serializes GPU work (DVD_INGEST_CONCURRENCY), so extra in-flight docs simply
# wait there in status "queued" instead of fighting over the card.
while ($next -lt $files.Count -or $inflight.Count -gt 0) {
    # Fill the prefetch window.
    while (-not $stopSubmitting -and $inflight.Count -lt $Prefetch -and $next -lt $files.Count) {
        $file = $files[$next]
        $index = $next + 1
        $next++
        Write-Host "[$index/$($files.Count)] Uploading $($file.Name)"
        try {
            $upload = Invoke-Upload $file
            Write-Host "  job: $($upload.job_id)"
            $inflight.Add([pscustomobject]@{
                File       = $file
                Index      = $index
                JobId      = $upload.job_id
                Deadline   = (Get-Date).AddMinutes($JobTimeoutMinutes)
                LastDetail = $null
            }) | Out-Null
        }
        catch {
            $message = [string]$_.Exception.Message
            Write-Host "  failed: $message" -ForegroundColor Red
            $failures.Add([pscustomobject]@{ file = $file.Name; error = $message }) | Out-Null
            $completed++
            if ($StopOnError) { $stopSubmitting = $true }
        }
    }

    if ($inflight.Count -eq 0) {
        if ($stopSubmitting) { break }
        continue
    }

    Start-Sleep -Seconds $PollSeconds

    # Poll every in-flight job once, keeping those still running.
    $stillRunning = New-Object System.Collections.Generic.List[object]
    $sumFraction = 0.0
    $activeDetail = $null
    $activeFraction = 0.0
    foreach ($item in $inflight) {
        if ((Get-Date) -gt $item.Deadline) {
            Write-Host "  timeout: [$($item.Index)/$($files.Count)] $($item.File.Name) after $JobTimeoutMinutes min" -ForegroundColor Red
            $failures.Add([pscustomobject]@{ file = $item.File.Name; error = "job timeout after $JobTimeoutMinutes minutes" }) | Out-Null
            $completed++
            if ($StopOnError) { $stopSubmitting = $true }
            continue
        }

        try {
            $job = Invoke-RestMethod -Method GET -Uri (Join-Url $BaseUrl "documents/$($item.JobId)") -TimeoutSec 60
        }
        catch {
            # Transient poll error — keep the job and retry on the next tick.
            $stillRunning.Add($item) | Out-Null
            continue
        }

        $p = Get-JobProgress $job

        # Echo each new stage to the console (Write-Progress is invisible in captured logs).
        if ($p.Detail -ne $item.LastDetail) {
            Write-Host "    [$($item.Index)/$($files.Count)] $($item.File.Name): $($p.Detail)"
            $item.LastDetail = $p.Detail
        }

        if ($p.Status -eq "done") {
            Write-Host "  done: [$($item.Index)/$($files.Count)] name='$($job.name)' version='$($job.version)' nodes=$($job.nodes)"
            $results.Add([pscustomobject]@{
                file    = $item.File.Name
                job_id  = $item.JobId
                status  = "done"
                name    = $job.name
                version = $job.version
                nodes   = $job.nodes
            }) | Out-Null
            $completed++
        }
        elseif ($p.Status -eq "error") {
            Write-Host "  failed: [$($item.Index)/$($files.Count)] $($job.error)" -ForegroundColor Red
            $failures.Add([pscustomobject]@{ file = $item.File.Name; error = [string]$job.error }) | Out-Null
            $completed++
            if ($StopOnError) { $stopSubmitting = $true }
        }
        else {
            $stillRunning.Add($item) | Out-Null
            $sumFraction += $p.Fraction
            # The document actually holding the GPU drives the child bar; others sit in "queued".
            if ($p.Status -eq "processing") {
                $activeDetail = "[$($item.Index)/$($files.Count)] $($item.File.Name): $($p.Detail)"
                $activeFraction = $p.Fraction
            }
        }
    }
    $inflight = $stillRunning

    # Overall progress across the whole batch: finished files + partial progress of the rest.
    $overall = [int]([Math]::Min(100.0, ($completed + $sumFraction) * 100 / [Math]::Max($files.Count, 1)))
    Write-Progress -Id 1 -Activity "Uploading docs_data to $BaseUrl" -Status "done $completed/$($files.Count)" -PercentComplete $overall
    if ($activeDetail) {
        Write-Progress -Id 2 -ParentId 1 -Activity "current document" -Status $activeDetail -PercentComplete ([int]($activeFraction * 100))
    }
}

Write-Progress -Id 2 -ParentId 1 -Activity "current document" -Completed
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

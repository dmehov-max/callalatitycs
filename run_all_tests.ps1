$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

Get-ChildItem test_audio\*.mp3 | ForEach-Object {
    $outFile = "results\$($_.BaseName).txt"
    Write-Host "Processing:" $_.Name
    python test_single_call.py $_.FullName 2>&1 | Out-File -FilePath $outFile -Encoding utf8
}

Write-Host "DONE with all files."

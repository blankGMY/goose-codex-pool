Get-CimInstance Win32_Process -Filter "Name='codex.exe'" | ForEach-Object { $_.ProcessId; $_.ExecutablePath; $_.CommandLine; "" }
Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe'" | Select-Object -First 1 -ExpandProperty ExecutablePath

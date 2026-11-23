function Initialize-Esp32SigningKey {
    param([Parameter(Mandatory)][string]$ProjectDir)
    $ErrorActionPreference = 'Stop'
    $dir = Join-Path $ProjectDir 'secrets'
    [IO.Directory]::CreateDirectory($dir) | Out-Null
    $store = Join-Path $dir 'esp32_auth_key.dpapi'
    $dotenv = Join-Path $ProjectDir '.env'
    $utf8 = [Text.UTF8Encoding]::new($false)
    $lock = [IO.File]::Open("$store.lock", 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        $raw = if ([IO.File]::Exists($dotenv)) { [IO.File]::ReadAllText($dotenv) } else { '' }
        $pattern = '(?im)^[ \t]*ESP32_AUTH_KEY[ \t]*=([^\r\n]*)(?:\r?\n|$)'
        $entries = [regex]::Matches($raw, $pattern)
        $legacy = $null
        foreach ($entry in $entries) {
            $value = $entry.Groups[1].Value.Trim().Trim('"').Trim("'")
            if ($value -and $legacy -and $value -cne $legacy) { throw '存在多个不同的设备签名密钥，请先确认有效值。' }
            if ($value) { $legacy = $value }
        }

        if ([IO.File]::Exists($store)) {
            $secure = ConvertTo-SecureString ([IO.File]::ReadAllText($store))
            $stored = [Net.NetworkCredential]::new('', $secure).Password
            if (-not $stored -or ($legacy -and $legacy -cne $stored)) { throw '设备签名密钥存储与旧配置不一致。' }
        } else {
            $stored = $legacy
            if (-not $stored) { $stored = $env:ESP32_AUTH_KEY }
            if (-not $stored) {
                $bytes = New-Object byte[] 32
                $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
                try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
                $stored = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
            }
            $secure = ConvertTo-SecureString $stored -AsPlainText -Force
            $cipher = ConvertFrom-SecureString $secure
            $temp = "$store.$([guid]::NewGuid()).tmp"
            try {
                [IO.File]::WriteAllText($temp, $cipher, $utf8)
                [IO.File]::Move($temp, $store)
            } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
        }

        if ($entries.Count) {
            $temp = "$dotenv.$([guid]::NewGuid()).tmp"
            try {
                [IO.File]::WriteAllText($temp, [regex]::Replace($raw, $pattern, ''), $utf8)
                [IO.File]::Replace($temp, $dotenv, [NullString]::Value)
            } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
        }
        if (-not $env:ESP32_AUTH_KEY) { $env:ESP32_AUTH_KEY = $stored }
    } finally { $lock.Dispose() }
}

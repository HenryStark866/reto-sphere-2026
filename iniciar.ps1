# Arranque del sistema en un clic. Se lanza desde INICIAR.bat.
#
# Hace lo mismo que el comando del README, y ademas comprueba antes lo que
# suele fallar: el puerto todavia ocupado por un arranque anterior, documentos
# de prueba que quedaron colgados en el indice, y la cuota del proveedor.

$ErrorActionPreference = 'Continue'
$raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $raiz

function Titulo($t) { Write-Host ""; Write-Host "  $t" -ForegroundColor Cyan }
function Bien($t)   { Write-Host "    $t" -ForegroundColor Green }
function Mal($t)    { Write-Host "    $t" -ForegroundColor Red }
function Aviso($t)  { Write-Host "    $t" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  SEGUIMIENTO POSTOPERATORIO POR VOZ" -ForegroundColor White
Write-Host "  ----------------------------------" -ForegroundColor DarkGray

# --------------------------------------------------------------- requisitos
Titulo "Requisitos"

$python = Join-Path $raiz ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  Mal "Falta el entorno virtual."
  Write-Host "    Crealo con:  python -m venv .venv" -ForegroundColor DarkGray
  Write-Host "                 .venv\Scripts\pip install -r requirements.txt" -ForegroundColor DarkGray
  exit 1
}
Bien "Entorno virtual listo"

$env_file = Join-Path $raiz ".env"
if (-not (Test-Path $env_file)) {
  Mal "Falta el archivo .env"
  Write-Host "    Copia .env.example a .env y pon tu llave de console.groq.com" -ForegroundColor DarkGray
  exit 1
}
$llave = (Select-String -Path $env_file -Pattern '^GROQ_API_KEY=(.+)$' -ErrorAction SilentlyContinue)
if (-not $llave) {
  Mal "El .env no tiene GROQ_API_KEY con valor"
  Write-Host "    La consola funcionaria, pero la llamada de voz no." -ForegroundColor DarkGray
  exit 1
}
Bien "Credencial de Groq presente"

# ------------------------------------------------------------------- puerto
# Un arranque anterior que siguiera vivo deja el puerto tomado y el nuevo
# proceso muere con "solo se permite un uso de cada direccion de socket".
Titulo "Puerto 8000"
$previos = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like "*uvicorn*" }
if ($previos) {
  $previos | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
  Bien "Se cerro un servidor anterior que seguia corriendo"
} else {
  Bien "Libre"
}

# ----------------------------------------------------------------- arranque
Titulo "Arrancando"
Write-Host "    Se abre una ventana aparte con el registro del servidor." -ForegroundColor DarkGray
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:LLM_REINTENTOS = "8"
Start-Process -FilePath $python `
  -ArgumentList "-m", "uvicorn", "app.server:app", "--port", "8000" `
  -WorkingDirectory $raiz

$listo = $false
foreach ($i in 1..60) {
  Start-Sleep -Seconds 2
  try {
    $salud = Invoke-RestMethod "http://localhost:8000/api/salud" -TimeoutSec 3
    $listo = $true
    break
  } catch { Write-Host "." -NoNewline -ForegroundColor DarkGray }
}
Write-Host ""
if (-not $listo) {
  Mal "El servidor no respondio en 2 minutos. Mira la ventana del registro."
  exit 1
}
Bien "En pie"

# ------------------------------------------------------------ comprobaciones
Titulo "Estado"

$permitidos = $true
foreach ($k in $salud.modelos.PSObject.Properties.Name) {
  $m = $salud.modelos.$k
  if (-not $m.familia_permitida) { $permitidos = $false; Mal "$k = $($m.modelo) NO PERMITIDO" }
}
if ($permitidos) { Bien "Los tres modelos son de familia permitida" }

Write-Host "    Indice: $($salud.indice.documentos) documentos, $($salud.indice.fragmentos) fragmentos (v$($salud.indice.version))"
if ($salud.indice.documentos -ne 107) {
  Aviso "El corpus del reto son 107 documentos. Ahora hay $($salud.indice.documentos)."
}

# Un documento de una prueba anterior arruina la demostracion de conocimiento
# vivo: el contador ya no parte de 107 y la consulta previa a la subida podria
# encontrarlo.
try {
  $docs = Invoke-RestMethod "http://localhost:8000/api/documentos" -TimeoutSec 5
  $subidos = @($docs.documentos | Where-Object { $_.origen -eq 'consola' })
  if ($subidos.Count -gt 0) {
    Aviso "Hay $($subidos.Count) documento(s) subido(s) de pruebas anteriores:"
    $subidos | ForEach-Object { Write-Host "      - $($_.nombre)" -ForegroundColor Yellow }
    Aviso "Eliminalos desde la consola antes de grabar la demo."
  } else {
    Bien "Sin documentos de prueba colgados: la demo de alta y baja parte limpia"
  }
} catch { }

# La cuota del nivel gratuito es lo que puede tumbar una grabacion a la mitad.
# Cuando se agota, la extraccion falla en silencio y TODO sale verde.
try {
  $k = ($llave.Matches[0].Groups[1].Value).Trim()
  $cuerpo = @{ model = "llama-3.1-8b-instant"
               messages = @(@{ role = "user"; content = ("sintoma " * 300) })
               max_tokens = 5 } | ConvertTo-Json -Depth 5 -Compress
  $r = Invoke-RestMethod "https://api.groq.com/openai/v1/chat/completions" -Method Post `
        -Headers @{ Authorization = "Bearer $k" } -ContentType "application/json" `
        -Body $cuerpo -TimeoutSec 20
  Bien "Cuota de Groq disponible"
} catch {
  Aviso "Groq no acepto la peticion de prueba. Puede ser la cuota diaria."
  Aviso "Sintoma durante la demo: el agente responde al instante y todo sale verde."
}

# ------------------------------------------------------------------ navegador
Titulo "Abriendo el navegador"
Start-Process "http://localhost:8000"
Bien "http://localhost:8000"

Write-Host ""
Write-Host "  TODO LISTO" -ForegroundColor Green
Write-Host "    Llamada:  http://localhost:8000/llamada" -ForegroundColor DarkGray
Write-Host "    Consola:  http://localhost:8000/consola" -ForegroundColor DarkGray
Write-Host "    Salud:    http://localhost:8000/api/salud" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Para detenerlo: cierra la ventana del registro del servidor." -ForegroundColor DarkGray
Write-Host ""
Start-Sleep -Seconds 4

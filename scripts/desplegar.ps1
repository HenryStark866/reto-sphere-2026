<#
.SYNOPSIS
    Despliega el agente en Cloud Run y lo pone detras de Firebase Hosting.

.DESCRIPTION
    Firebase Hosting solo sirve archivos estaticos, asi que la aplicacion corre
    en Cloud Run y Hosting reescribe todo el trafico hacia el servicio. La
    imagen se construye con Cloud Build, de modo que no hace falta Docker local.

    Requisitos previos que NO hace este script porque no debe hacerlos una
    automatizacion:
      - Plan Blaze activo en el proyecto de Firebase (requiere tarjeta).
      - La llave de Groq, que se guarda en Secret Manager, nunca en la imagen.

.PARAMETER Proyecto
    ID del proyecto de Google Cloud / Firebase. Obligatorio y sin valor por
    defecto a proposito: desplegar en el proyecto equivocado es caro de deshacer.

.EXAMPLE
    ./scripts/desplegar.ps1 -Proyecto sara-postop-2026 -LlaveGroq gsk_xxx
#>
param(
    [Parameter(Mandatory = $true)][string]$Proyecto,
    [string]$Region = "us-central1",
    [string]$Servicio = "sara-postop",
    [string]$LlaveGroq = "",
    [switch]$SaltarHosting
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host "== Proyecto: $Proyecto | Region: $Region | Servicio: $Servicio ==" -ForegroundColor Cyan

# --- comprobaciones previas ------------------------------------------------
if (-not (Test-Path "$raiz/index/vectores.npy")) {
    throw "Falta el indice construido. Ejecuta: python -m scripts.build_index"
}
$mb = [math]::Round((Get-ChildItem "$raiz/index" | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "Indice presente: $mb MB" -ForegroundColor Green

gcloud config set project $Proyecto | Out-Null

Write-Host "`n== Habilitando APIs ==" -ForegroundColor Cyan
gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
    artifactregistry.googleapis.com secretmanager.googleapis.com

# --- la llave de Groq va a Secret Manager, no a la imagen -------------------
if ($LlaveGroq) {
    Write-Host "`n== Guardando GROQ_API_KEY en Secret Manager ==" -ForegroundColor Cyan
    $existe = gcloud secrets list --filter="name:groq-api-key" --format="value(name)" 2>$null
    if (-not $existe) {
        gcloud secrets create groq-api-key --replication-policy=automatic
    }
    $tmp = New-TemporaryFile
    # Sin salto de linea final: uno solo rompe la cabecera Authorization.
    [System.IO.File]::WriteAllText($tmp.FullName, $LlaveGroq)
    gcloud secrets versions add groq-api-key --data-file=$tmp.FullName
    Remove-Item $tmp.FullName -Force

    $numero = gcloud projects describe $Proyecto --format="value(projectNumber)"
    gcloud secrets add-iam-policy-binding groq-api-key `
        --member="serviceAccount:$numero-compute@developer.gserviceaccount.com" `
        --role="roles/secretmanager.secretAccessor" | Out-Null
}

# --- construir y desplegar --------------------------------------------------
Write-Host "`n== Construyendo la imagen con Cloud Build ==" -ForegroundColor Cyan
$imagen = "gcr.io/$Proyecto/${Servicio}:$(Get-Date -Format 'yyyyMMdd-HHmmss')"
gcloud builds submit --tag $imagen --timeout=20m

Write-Host "`n== Desplegando en Cloud Run ==" -ForegroundColor Cyan
# --max-instances=1 NO es tacaneria: el indice vivo y las llamadas en curso
# viven en la memoria del proceso. Con dos instancias, un documento subido a la
# instancia A no existiria para la B, y una llamada iniciada en A se caeria al
# llegar a B. Escalar de verdad exigiria mover el indice y la sesion fuera del
# proceso; para la evaluacion, una instancia fija es lo correcto.
#
# --min-instances=1 evita el arranque en frio: cargar el modelo ONNX y el
# indice toma varios segundos y el primer paciente los pagaria como silencio.
$argumentos = @(
    "run", "deploy", $Servicio,
    "--image", $imagen,
    "--region", $Region,
    "--platform", "managed",
    "--allow-unauthenticated",
    "--memory", "2Gi",
    "--cpu", "1",
    "--min-instances", "1",
    "--max-instances", "1",
    "--concurrency", "20",
    "--timeout", "300",
    "--port", "8080"
)
if ($LlaveGroq) {
    $argumentos += @("--set-secrets", "GROQ_API_KEY=groq-api-key:latest")
}
& gcloud @argumentos

$url = gcloud run services describe $Servicio --region $Region --format="value(status.url)"
Write-Host "`nCloud Run: $url" -ForegroundColor Green

# --- verificacion -----------------------------------------------------------
Write-Host "`n== Verificando ==" -ForegroundColor Cyan
$salud = Invoke-RestMethod "$url/api/salud" -TimeoutSec 60
"  credenciales groq : $($salud.credenciales_groq)"
"  corpus listo      : $($salud.corpus_listo)"
"  documentos        : $($salud.indice.documentos)"
"  fragmentos        : $($salud.indice.fragmentos)"
"  modelo dialogo    : $($salud.modelos.dialogo.modelo) (permitido: $($salud.modelos.dialogo.familia_permitida))"
if (-not $salud.ok) {
    Write-Warning "El servicio responde pero /api/salud reporta ok=false. Revisa lo de arriba."
}

# --- Firebase Hosting delante ----------------------------------------------
if (-not $SaltarHosting) {
    Write-Host "`n== Publicando Firebase Hosting ==" -ForegroundColor Cyan
    firebase deploy --only hosting --project $Proyecto
}

Write-Host "`nListo. Cloud Run: $url" -ForegroundColor Green

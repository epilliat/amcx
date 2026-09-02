# Installation d'AMCx en une commande — Windows (PowerShell).
#
#   irm https://raw.githubusercontent.com/epilliat/amcx/main/bootstrap.ps1 | iex
#
# Ne demande AUCUN prerequis : si Python manque, uv l'installe. Rien n'est pose
# hors du dossier utilisateur, aucun droit administrateur n'est requis.
# Pour une branche precise : $env:AMCX_REF = "ma-branche" avant de lancer.

$ErrorActionPreference = "Stop"

$Repo = "https://github.com/epilliat/amcx"
$Ref  = if ($env:AMCX_REF) { $env:AMCX_REF } else { "main" }

Write-Host ""
Write-Host "AMCx - installation"
Write-Host "==================="

# 1) uv : gestionnaire d'environnements Python (installe Python au besoin).
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "-> uv deja present ($(uv --version))"
} else {
    Write-Host "-> Installation de uv (gestionnaire Python, dans ton dossier utilisateur)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    # Rendre uv utilisable DANS ce script, sans attendre un nouveau terminal.
    # L'installateur respecte UV_INSTALL_DIR ; sinon %USERPROFILE%\.local\bin.
    $candidates = @()
    if ($env:UV_INSTALL_DIR) { $candidates += $env:UV_INSTALL_DIR }
    $candidates += (Join-Path $env:USERPROFILE ".local\bin")
    foreach ($d in $candidates) {
        if (Test-Path (Join-Path $d "uv.exe")) {
            $env:Path = "$d;$env:Path"
            break
        }
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "X uv introuvable apres installation. Ouvre un nouveau PowerShell et relance."
    exit 1
}

# 2) AMCx, avec son propre Python isole.
Write-Host "-> Installation d'AMCx depuis $Repo ($Ref)..."
uv tool install --force "git+$Repo@$Ref"

# 3) Rendre `amcx` utilisable : maintenant et dans les prochains terminaux.
try { uv tool update-shell | Out-Null } catch { }
$AmcxBin = $null
$binDirs = @()
if ($env:UV_TOOL_BIN_DIR) { $binDirs += $env:UV_TOOL_BIN_DIR }
$binDirs += (Join-Path $env:USERPROFILE ".local\bin")
foreach ($d in $binDirs) {
    $candidate = Join-Path $d "amcx.exe"
    if (Test-Path $candidate) {
        $AmcxBin = $candidate
        $env:Path = "$d;$env:Path"
        break
    }
}
if (-not $AmcxBin) {
    $cmd = Get-Command amcx -ErrorAction SilentlyContinue
    if ($cmd) { $AmcxBin = $cmd.Source }
}

# 4) pdflatex : necessaire seulement pour CREER un sujet.
if (-not (Get-Command pdflatex -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "/!\ pdflatex n'est pas installe."
    Write-Host "    AMCx fonctionne sans (correction de copies d'un projet deja compile),"
    Write-Host "    mais la creation d'un sujet en a besoin : https://miktex.org/download"
    Write-Host "    Le style AMC lui-meme est fourni avec AMCx : rien d'autre a installer."
    Write-Host "    MiKTeX telechargera les paquets LaTeX manquants tout seul."
}

Write-Host ""
if ($AmcxBin) {
    Write-Host "-> Diagnostic..."
    Write-Host ""
    try { & $AmcxBin doctor } catch { }
} else {
    Write-Host "/!\ La commande amcx n'a pas ete trouvee apres installation."
    Write-Host "    Ouvre un nouveau PowerShell puis lance : amcx doctor"
}

Write-Host ""
Write-Host "==================================================================="
Write-Host "OK AMCx est installe."
Write-Host ""
Write-Host "   amcx              demarre le serveur, puis http://localhost:5050/"
Write-Host "   amcx --version    version installee"
Write-Host "   amcx doctor       diagnostic (a envoyer en cas de probleme)"
Write-Host "   amcx update       mise a jour"
Write-Host ""
Write-Host "Si 'amcx' n'est pas reconnu, ferme et rouvre PowerShell."
Write-Host "==================================================================="

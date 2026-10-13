<#
.SYNOPSIS
  Starts the SRC Streamlit dashboard on http://localhost:8501 (this PC only).
.PARAMETER Address
  Bind address. Default 127.0.0.1; use 0.0.0.0 to expose it on the network.
.NOTES
  Runs in the foreground; Ctrl+C stops it. Reads data/src_dashboard.duckdb or data/src.duckdb
  as configured by the CLI. Run `uv run src run -u observed` separately to refresh the data.
#>
param([string]$Address = '127.0.0.1')

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "Dashboard: http://localhost:8501 (bind $Address)"
uv run src dashboard --address $Address

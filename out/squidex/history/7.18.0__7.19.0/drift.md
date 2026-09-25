# Architecture drift report  (commit 7.18.0..7.19.0)

## New code facts not represented in model
- new target `csharp:csproj:frontend/generator/Generator/Generator.csproj`
- new edge `csharp:csproj:backend/src/Migrations/Migrations.csproj` → `csharp:csproj:backend/extensions/Squidex.Extensions/Squidex.Extensions.csproj`

## Model elements no longer found in code
- _none_

## Suspicious dependencies (declared but unused / layering violations)
- _none_

# Architecture drift report  (commit 7.17.0..7.18.0)

## New code facts not represented in model
- new target `csharp:csproj:backend/src/Squidex.Data.EntityFramework/Squidex.Data.EntityFramework.csproj`
- new target `csharp:csproj:backend/tests/Squidex.Data.Tests.CodeGenerator/Squidex.Data.Tests.CodeGenerator.csproj`
- new edge `csharp:csproj:backend/src/Squidex.Data.EntityFramework/Squidex.Data.EntityFramework.csproj` → `csharp:csproj:backend/src/Squidex.Domain.Apps.Entities/Squidex.Domain.Apps.Entities.csproj`
- new edge `csharp:csproj:backend/src/Squidex.Data.EntityFramework/Squidex.Data.EntityFramework.csproj` → `csharp:csproj:backend/src/Squidex.Domain.Users/Squidex.Domain.Users.csproj`
- new edge `csharp:csproj:backend/src/Squidex.Data.EntityFramework/Squidex.Data.EntityFramework.csproj` → `csharp:csproj:backend/src/Squidex.Infrastructure/Squidex.Infrastructure.csproj`
- new edge `csharp:csproj:backend/src/Squidex/Squidex.csproj` → `csharp:csproj:backend/src/Squidex.Data.EntityFramework/Squidex.Data.EntityFramework.csproj`
- new edge `csharp:csproj:backend/tests/Squidex.Data.Tests/Squidex.Data.Tests.csproj` → `csharp:csproj:backend/src/Squidex.Data.EntityFramework/Squidex.Data.EntityFramework.csproj`
- new edge `csharp:csproj:backend/tests/Squidex.Data.Tests/Squidex.Data.Tests.csproj` → `csharp:csproj:backend/tests/Squidex.Data.Tests.CodeGenerator/Squidex.Data.Tests.CodeGenerator.csproj`

## Model elements no longer found in code
- _none_

## Suspicious dependencies (declared but unused / layering violations)
- _none_
